"""
Agent Container Manager - Manages per-user OpenClaw agent containers.

Handles:
- Per-user workspace creation with API tokens
- Docker container lifecycle (start/stop/cleanup)
- WebSocket communication bridge to OpenClaw gateway
- Streaming message relay
"""

import json
import logging
import os
import secrets
import time
from pathlib import Path

import docker
import websockets.sync.client as ws_client
from django.conf import settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from .models import AgentMessage, AgentSession, ChatSession

logger = logging.getLogger(__name__)

# Container configuration
AGENT_IMAGE = os.environ.get('AGENT_IMAGE', 'adma-openclaw-agent:latest')
AGENT_NETWORK = os.environ.get('AGENT_NETWORK', 'adma_network')
AGENT_PORT_RANGE_START = int(os.environ.get('AGENT_PORT_START', '19000'))
AGENT_PORT_RANGE_END = int(os.environ.get('AGENT_PORT_END', '19999'))
AGENT_IDLE_TIMEOUT_MINUTES = int(os.environ.get('AGENT_IDLE_TIMEOUT', '30'))
AGENT_WORKSPACES_DIR = Path(os.environ.get(
    'AGENT_WORKSPACES_DIR',
    '/opt/adma/agent_workspaces'
))
AGENT_WORKSPACES_VOLUME = os.environ.get(
    'AGENT_WORKSPACES_VOLUME', 'adma_geo_adma_agent_workspaces'
)
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')


def _get_docker_client():
    """Get a Docker client connected to the Docker daemon."""
    return docker.from_env()


def _ensure_workspace(user):
    """
    Create per-user workspace directory with API token credentials.
    Also creates an openclaw state directory for persistent agent memory/sessions.
    Returns the workspace path.
    """
    workspace = AGENT_WORKSPACES_DIR / str(user.id)
    credentials_dir = workspace / 'credentials'
    code_dir = workspace / 'code'
    openclaw_state_dir = workspace / 'openclaw_state'

    credentials_dir.mkdir(parents=True, exist_ok=True)
    code_dir.mkdir(parents=True, exist_ok=True)
    openclaw_state_dir.mkdir(parents=True, exist_ok=True)

    # Get or create the user's API token
    token, _ = Token.objects.get_or_create(user=user)

    # Determine the ADMA base URL (internal Docker network address)
    adma_base_url = os.environ.get('ADMA_INTERNAL_URL', 'http://django:8000')

    # Write credentials file
    credentials_file = credentials_dir / 'adma_token.json'
    credentials_data = {
        'token': token.key,
        'base_url': f'{adma_base_url}/api/v1/',
        'web_url': os.environ.get('ADMA_PUBLIC_URL', 'https://adma.unl.edu'),
        'username': user.username,
        'user_id': str(user.id),
    }
    credentials_file.write_text(json.dumps(credentials_data, indent=2))

    return workspace


def _allocate_port():
    """Find an available port for a new agent container."""
    used_ports = set(
        AgentSession.objects.filter(
            status__in=['running', 'starting']
        ).values_list('container_port', flat=True)
    )

    for port in range(AGENT_PORT_RANGE_START, AGENT_PORT_RANGE_END + 1):
        if port not in used_ports:
            return port

    raise RuntimeError('No available ports for agent container')


class AgentContainerManager:
    """Manages per-user OpenClaw agent Docker containers."""

    def get_or_create_session(self, user):
        """Get or create the user's agent session."""
        session, _ = AgentSession.objects.get_or_create(user=user)
        return session

    def start_agent(self, user):
        """Start an OpenClaw agent container for the user."""
        session = self.get_or_create_session(user)

        # If already running, just return
        if session.status == 'running' and session.container_id:
            try:
                client = _get_docker_client()
                container = client.containers.get(session.container_id)
                if container.status == 'running':
                    session.last_activity = timezone.now()
                    session.save(update_fields=['last_activity'])
                    return session
            except (docker.errors.NotFound, docker.errors.APIError):
                pass  # Container is gone, restart

        # Prepare workspace
        workspace = _ensure_workspace(user)
        port = _allocate_port()
        gateway_token = secrets.token_urlsafe(32)

        # Update session to starting
        session.status = 'starting'
        session.container_port = port
        session.gateway_token = gateway_token
        session.error_message = ''
        session.save(update_fields=['status', 'container_port', 'gateway_token', 'error_message'])

        try:
            client = _get_docker_client()

            # Remove any stale container with the same name
            container_name = f'adma-agent-{user.id}'
            try:
                old = client.containers.get(container_name)
                old.remove(force=True)
            except docker.errors.NotFound:
                pass

            # Start the container
            # Mount the shared Docker volume containing all user workspaces,
            # then set AGENT_USER_DIR so the entrypoint/agent uses the right subdir
            container = client.containers.run(
                image=AGENT_IMAGE,
                name=container_name,
                detach=True,
                network=AGENT_NETWORK,
                environment={
                    'OPENCLAW_GATEWAY_TOKEN': gateway_token,
                    'OPENAI_API_KEY': OPENAI_API_KEY,
                    'OPENCLAW_GATEWAY_PORT': '18789',
                    'AGENT_USER_DIR': str(user.id),
                    'OPENCLAW_STATE_DIR': '/persistent_state',
                },
                volumes={
                    AGENT_WORKSPACES_VOLUME: {'bind': '/agent_workspaces', 'mode': 'rw'},
                },
                ports={'18789/tcp': ('0.0.0.0', port)},
                mem_limit='1g',
                cpu_quota=100000,  # 1 CPU
                restart_policy={'Name': 'unless-stopped'},
            )

            session.container_id = container.id
            session.status = 'running'
            session.started_at = timezone.now()
            session.last_activity = timezone.now()
            session.save(update_fields=[
                'container_id', 'status', 'started_at', 'last_activity'
            ])

            # Wait for gateway to be ready
            self._wait_for_gateway(session, timeout=30)

            return session

        except Exception as e:
            logger.error(f'Failed to start agent for {user.username}: {e}')
            session.status = 'error'
            session.error_message = str(e)
            session.save(update_fields=['status', 'error_message'])
            raise

    def _wait_for_gateway(self, session, timeout=30):
        """Wait for the OpenClaw gateway to become ready."""
        import socket

        host = self._get_container_host(session)
        port = session.container_port
        start = time.time()

        while time.time() - start < timeout:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex((host, port))
                sock.close()
                if result == 0:
                    # Give gateway a moment to fully initialize
                    time.sleep(2)
                    return True
            except socket.error:
                pass
            time.sleep(1)

        logger.warning(f'Gateway not ready after {timeout}s for {session.user.username}')
        return False

    def _get_container_host(self, session):
        """Get the host address for connecting to a container."""
        # When Django runs inside Docker, connect via container name
        # When running locally, connect via localhost
        if os.environ.get('RUNNING_IN_DOCKER'):
            return f'adma-agent-{session.user.id}'
        return '127.0.0.1'

    def stop_agent(self, user):
        """Stop and remove the user's agent container."""
        try:
            session = AgentSession.objects.get(user=user)
        except AgentSession.DoesNotExist:
            return

        if session.container_id:
            try:
                client = _get_docker_client()
                container = client.containers.get(session.container_id)
                container.stop(timeout=10)
                container.remove()
            except (docker.errors.NotFound, docker.errors.APIError) as e:
                logger.warning(f'Error stopping container for {user.username}: {e}')

        session.status = 'stopped'
        session.container_id = ''
        session.container_port = None
        session.gateway_token = ''
        session.save(update_fields=['status', 'container_id', 'container_port', 'gateway_token'])

    def ensure_agent(self, user):
        """Ensure the user's agent is running. Start if needed."""
        session = self.get_or_create_session(user)

        if session.status == 'running' and session.container_id:
            try:
                client = _get_docker_client()
                container = client.containers.get(session.container_id)
                if container.status == 'running':
                    session.last_activity = timezone.now()
                    session.save(update_fields=['last_activity'])
                    return session
            except (docker.errors.NotFound, docker.errors.APIError):
                pass

        return self.start_agent(user)

    def get_status(self, user):
        """Get the current agent status for a user."""
        try:
            session = AgentSession.objects.get(user=user)
            result = {
                'status': session.status,
                'started_at': session.started_at.isoformat() if session.started_at else None,
                'last_activity': session.last_activity.isoformat() if session.last_activity else None,
            }
            if session.status == 'error':
                result['error'] = session.error_message
            return result
        except AgentSession.DoesNotExist:
            return {'status': 'stopped'}

    def send_message(self, user, message_text):
        """
        Send a message to the user's agent and return the full response.
        Non-streaming version.
        """
        chunks = []
        for chunk in self.send_message_stream(user, message_text):
            if chunk.get('type') == 'text':
                chunks.append(chunk['content'])
            elif chunk.get('type') == 'error':
                raise RuntimeError(chunk['content'])
        return ''.join(chunks)

    def send_message_async(self, user, message_text, chat_session_id=None):
        """
        Send a message to the user's agent asynchronously via Celery.
        Returns the user message ID which can be polled for a response.
        """
        session = self.ensure_agent(user)

        # Get or create chat session
        if chat_session_id:
            try:
                chat_session = ChatSession.objects.get(id=chat_session_id, user=user)
            except ChatSession.DoesNotExist:
                chat_session = ChatSession.objects.create(user=user, name='New Chat')
        else:
            chat_session = ChatSession.objects.create(user=user, name='New Chat')

        # Update chat session timestamp
        chat_session.save()  # triggers auto_now on updated_at

        # Save the user message
        user_msg = AgentMessage.objects.create(
            session=session,
            chat_session=chat_session,
            role='user',
            content=message_text,
        )

        # Dispatch to Celery — pass chat_session_id as the openclaw session-id
        from .tasks import run_agent_message_task
        run_agent_message_task.delay(
            str(user.id), str(session.id), message_text, str(chat_session.id)
        )

        return str(user_msg.id), str(chat_session.id)

    def get_chat_sessions(self, user):
        """Get all chat sessions for a user, oldest first (newest tab on right)."""
        return ChatSession.objects.filter(user=user, is_active=True).order_by('created_at')

    def rename_chat_session(self, user, chat_session_id, name):
        """Rename a chat session."""
        cs = ChatSession.objects.get(id=chat_session_id, user=user)
        cs.name = name
        cs.save(update_fields=['name'])
        return cs

    def delete_chat_session(self, user, chat_session_id):
        """Permanently delete a chat session, its messages, and the OpenClaw session file."""
        cs = ChatSession.objects.get(id=chat_session_id, user=user)
        AgentMessage.objects.filter(chat_session=cs).delete()

        # Also remove the OpenClaw session transcript
        session_file = (
            AGENT_WORKSPACES_DIR / str(user.id) / 'openclaw_state'
            / 'agents' / 'main' / 'sessions' / f'{chat_session_id}.jsonl'
        )
        if session_file.exists():
            session_file.unlink()

        cs.delete()

    def get_chat_history(self, user, chat_session_id=None, limit=50):
        """Get chat history for a user, optionally filtered by chat session."""
        session = self.get_or_create_session(user)
        qs = AgentMessage.objects.filter(session=session)
        if chat_session_id:
            qs = qs.filter(chat_session_id=chat_session_id)
        messages = qs.order_by('-created_at')[:limit]
        return list(reversed(messages))


def cleanup_idle_agents():
    """Stop agent containers that have been idle for too long."""
    cutoff = timezone.now() - timezone.timedelta(minutes=AGENT_IDLE_TIMEOUT_MINUTES)
    idle_sessions = AgentSession.objects.filter(
        status='running',
        last_activity__lt=cutoff,
    )

    manager = AgentContainerManager()
    count = 0
    for session in idle_sessions:
        try:
            manager.stop_agent(session.user)
            count += 1
            logger.info(f'Stopped idle agent for {session.user.username}')
        except Exception as e:
            logger.error(f'Error stopping idle agent for {session.user.username}: {e}')

    return count
