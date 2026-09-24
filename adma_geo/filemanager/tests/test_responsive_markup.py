"""
The markup that decides whether a page fits a phone.

Two patterns pushed pages wider than a phone viewport, which made the whole
layout scroll sideways: a table with nowhere to scroll, and a flex row holding
a heading beside its buttons with no permission to wrap. Both are easy to
reintroduce and invisible on a desktop, so they are checked on the rendered
pages rather than left to review.
"""
import re

from django.contrib.auth import get_user_model
from django.test import TestCase

from filemanager.models import Folder

User = get_user_model()

PAGES = [
    '/dashboard/',
    '/tools/',
    '/profile/',
    '/tools/zonal-statistics/',
    '/tools/public-data/',
    '/tools/terrain/',
    '/tools/point-sampling/',
    '/tools/vector-ops/',
]


def bare_tables(html):
    """Tables with no .table-responsive ancestor within reach."""
    found = []
    for match in re.finditer(r'<table\b', html):
        preceding = html[max(0, match.start() - 400):match.start()]
        if 'table-responsive' not in preceding:
            found.append(html[match.start():match.start() + 80])
    return found


class ResponsiveMarkupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            'grower', password='correct-horse-battery', first_name='Pat'
        )
        self.client.login(username='grower', password='correct-horse-battery')
        Folder.objects.create(name='field-1', owner=self.user)

    def test_no_page_renders_a_table_with_nowhere_to_scroll(self):
        for page in PAGES:
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertEqual(response.status_code, 200, page)
                html = response.content.decode()
                self.assertEqual(bare_tables(html), [], page)

    def test_headings_and_their_buttons_may_wrap(self):
        """
        A heading and three buttons cannot sit side by side at 375 px. The row
        has to be allowed to wrap, or the buttons run off the screen.
        """
        html = self.client.get('/dashboard/').content.decode()

        for match in re.finditer(
            r'<div class="([^"]*d-flex[^"]*justify-content-between[^"]*)"', html
        ):
            classes = match.group(1)
            following = html[match.end():match.end() + 400]
            if not re.search(r'<h[12]\b', following):
                continue
            with self.subTest(classes=classes):
                self.assertIn('flex-wrap', classes)

    def test_the_viewport_meta_tag_is_present(self):
        """Without it a phone renders the page at desktop width and zooms out."""
        html = self.client.get('/dashboard/').content.decode()
        self.assertRegex(html, r'<meta[^>]+name="viewport"[^>]+width=device-width')
