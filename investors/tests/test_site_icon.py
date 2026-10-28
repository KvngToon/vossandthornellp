"""Every page used to show the browser's generic globe because the site had no favicon at all."""
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from investors.models import InvestorProfile

PW = 'correct-horse-battery-9'
STATIC = Path(settings.BASE_DIR) / 'static'


class IconFileTests(TestCase):
    def test_the_icon_set_exists_and_is_valid(self):
        ico = Image.open(STATIC / 'favicon.ico')
        self.assertEqual(ico.format, 'ICO')
        self.assertTrue({(16, 16), (32, 32), (48, 48)} <= set(ico.info['sizes']))
        touch = Image.open(STATIC / 'apple-touch-icon.png')
        self.assertEqual((touch.format, touch.size), ('PNG', (180, 180)))
        big = Image.open(STATIC / 'icon-512.png')
        self.assertEqual(big.size, (512, 512))

    def test_the_svg_is_well_formed_and_uses_the_brand_colours(self):
        root = ET.parse(STATIC / 'favicon.svg').getroot()
        self.assertTrue(root.tag.endswith('svg'))
        self.assertEqual(root.get('viewBox'), '0 0 512 512')
        text = (STATIC / 'favicon.svg').read_text()
        self.assertIn('rgb(201, 168, 76)', text)     # the site's gold (--accent)
        self.assertIn('rgb(7, 7, 13)', text)         # the site's near-black (--bg)

    def test_the_icons_look_like_the_brand_not_a_blank_square(self):
        img = Image.open(STATIC / 'icon-512.png').convert('RGBA')
        self.assertEqual(img.getpixel((0, 0))[3], 0)                      # rounded corner is transparent
        self.assertEqual(img.getpixel((256, 60))[:3], (7, 7, 13))         # dark field
        self.assertEqual(img.getpixel((180, 160))[:3], (201, 168, 76))    # inside the gold V

    def test_the_icons_are_small(self):
        for name in ('favicon.ico', 'favicon.svg', 'apple-touch-icon.png'):
            self.assertLess((STATIC / name).stat().st_size, 20_000, name)   # loaded on every page


class PagesDeclareTheIconTests(TestCase):
    def hrefs(self, response):
        html = response.content.decode()
        return re.findall(r'<link rel="(?:icon|apple-touch-icon|shortcut icon)"[^>]*href="([^"]+)"', html)

    def assert_icons(self, response, label):
        found = self.hrefs(response)
        self.assertGreaterEqual(len(found), 2, f'{label} declares no icon')
        for href in found:
            self.assertTrue(href.startswith(settings.STATIC_URL), (label, href))
            self.assertIsNotNone(finders.find(href[len(settings.STATIC_URL):]), f'{label}: {href} does not exist')

    def test_public_pages(self):
        for url in ('/', '/about/', '/contact/'):
            self.assert_icons(self.client.get(url), url)

    def test_the_investor_login_and_password_pages(self):
        for name in ('login', 'password_reset'):
            self.assert_icons(self.client.get(reverse(f'investors:{name}')), name)

    def test_signed_in_investor_pages(self):
        user = User.objects.create_user('jane', 'jane@example.com', PW)
        InvestorProfile.objects.create(user=user, display_name='Jane')
        self.client.login(username='jane', password=PW)
        for name in ('home', 'performance', 'documents', 'withdrawals', 'withdrawal_new'):
            self.assert_icons(self.client.get(reverse(f'investors:{name}')), name)

    def test_the_admin_has_an_icon_too(self):
        root = User.objects.create_superuser('root', 'r@corp.com', PW)
        self.assert_icons(self.client.get('/admin/login/'), 'admin login')
        self.client.force_login(root)
        self.assert_icons(self.client.get('/admin/'), 'admin index')

    def test_the_svg_is_listed_first_so_modern_browsers_prefer_it(self):
        html = self.client.get(reverse('investors:login')).content.decode()
        self.assertLess(html.index('favicon.svg'), html.index('favicon.ico'))
        self.assertIn('image/svg+xml', html)


class FaviconRouteTests(TestCase):
    def test_the_default_browser_request_is_redirected_to_the_static_file(self):
        r = self.client.get('/favicon.ico')
        self.assertEqual(r.status_code, 301)
        self.assertEqual(r['Location'], settings.STATIC_URL + 'favicon.ico')

    def test_it_is_not_a_404_any_more(self):
        self.assertNotEqual(self.client.get('/favicon.ico').status_code, 404)

    def test_the_deploy_build_step_collects_the_icons(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with override_settings(STATIC_ROOT=tmp):
            call_command('collectstatic', interactive=False, verbosity=0)
        for name in ('favicon.ico', 'favicon.svg', 'apple-touch-icon.png'):
            self.assertTrue((Path(tmp) / name).exists(), f'collectstatic did not collect {name}')
