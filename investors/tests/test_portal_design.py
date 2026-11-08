"""Guards for the portal's visual design: white paper, readable colours, and the plain-English balance sentence."""
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from investors.models import Investment, InvestorProfile, Transaction, ValuationSnapshot
from investors.templatetags.investor_extras import money_abs

PW = 'correct-horse-battery-9'
BASE = Path(settings.BASE_DIR) / 'investors' / 'templates' / 'investors' / 'base.html'


def tokens():
    css = BASE.read_text()
    return {name: value for name, value in re.findall(r'--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})', css)}


def luminance(hex_colour):
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class ColourTests(TestCase):
    def test_the_page_is_white(self):
        t = tokens()
        self.assertEqual(t['paper'].lower(), '#ffffff')
        css = BASE.read_text()
        self.assertRegex(css, r'body\s*\{[^}]*background:\s*var\(--paper\)')
        self.assertNotIn('#07070d', css)                                  # the old dark background is gone

    def test_every_text_colour_meets_wcag_aa_on_the_backgrounds_it_is_used_on(self):
        t = tokens()
        pairs = {
            'body text on paper': (t['ink'], t['paper']), 'muted text on paper': (t['slate'], t['paper']),
            'headings on paper': (t['oxford'], t['paper']), 'links on paper': (t['brass-deep'], t['paper']),
            'gains on paper': (t['evergreen'], t['paper']), 'losses on paper': (t['garnet'], t['paper']),
            'button text on button': ('#ffffff', t['oxford']), 'muted text on hover row': (t['slate'], t['mist']),
            'body text on hover row': (t['ink'], t['mist']), 'banner text on banner': (t['oxford'], '#faf6ea'),
            'selected chip text': ('#ffffff', t['oxford']),
        }
        for name, (fg, bg) in pairs.items():
            self.assertGreaterEqual(contrast(fg, bg), 4.5, f'{name}: {fg} on {bg} is only {contrast(fg, bg):.2f}:1')

    def test_the_decorative_brass_is_never_used_for_text(self):
        css = BASE.read_text()
        for line in css.splitlines():
            if re.search(r'(^|[\s;{])color:\s*var\(--brass\)', line):
                self.fail(f'--brass (low contrast on white) used as a text colour: {line.strip()}')

    def test_no_leftover_template_tells(self):
        css = BASE.read_text()
        self.assertNotIn('IBM Plex Mono', css)                            # no monospaced labels
        self.assertNotRegex(css, r'text-transform:\s*uppercase')           # no tracked all-caps labels
        self.assertNotIn('linear-gradient', css.replace('data:image', ''))  # no gradient washes

    def test_motion_respects_the_reduced_motion_preference(self):
        self.assertIn('prefers-reduced-motion', BASE.read_text())
        chart = (BASE.parent / '_chart_line.html').read_text()
        self.assertIn('prefers-reduced-motion', chart)

    def test_keyboard_focus_is_visible(self):
        self.assertIn(':focus-visible', BASE.read_text())


class PageTests(TestCase):
    def setUp(self):
        user = User.objects.create_user('jane', 'jane@example.com', PW)
        self.profile = InvestorProfile.objects.create(user=user, display_name='Jane Doe')
        self.client.login(username='jane', password=PW)

    def plan(self, principal='10000', days_ago=400):
        return Investment.objects.create(
            investor=self.profile, plan_name='Plan', principal=Decimal(principal), annual_rate=Decimal('10'),
            compounding='annual', start_date=date.today() - timedelta(days=days_ago),
            maturity_date=date.today() + timedelta(days=700))

    def hero(self):
        page = self.client.get(reverse('investors:home')).content.decode()
        return re.sub(r'\s+', ' ', re.search(r'class="hero__sentence">(.*?)</p>', page, re.S).group(1)).strip()

    def test_fonts_and_icons_are_declared_on_every_page_type(self):
        self.plan()
        self.client.logout()
        self.assertContains(self.client.get(reverse('investors:login')), 'Newsreader')          # the signed-out page too
        for url in (reverse('investors:home'), reverse('investors:performance'),
                    reverse('investors:withdrawals'), reverse('investors:documents')):
            self.client.login(username='jane', password=PW)
            html = self.client.get(url).content.decode()
            self.assertIn('Newsreader', html, url)
            self.assertIn('Hanken+Grotesk', html, url)
            self.assertIn('Voss &amp; Thorne', html, url)
            self.assertIn('Private and confidential', html, url)           # the quiet footer

    def test_the_masthead_has_the_brand_and_the_logo_mark(self):
        self.client.logout()
        html = self.client.get(reverse('investors:login')).content.decode()
        self.assertIn('class="brand__name"', html)
        self.assertRegex(html, r'<img src="[^"]*favicon\.svg" alt=""')   # decorative: empty alt, the name is beside it

    def test_the_hero_says_what_the_portfolio_is_worth_in_words(self):
        self.plan()
        page = self.client.get(reverse('investors:home')).content.decode()
        self.assertIn('your investments are worth', page)
        self.assertIn(f'As of {date.today():%-d %B %Y}', page)
        self.assertRegex(page, r'class="hero__figure">\$1[0-9],[0-9]{3}\.[0-9]{2}</p>')

    def test_hero_sentence_for_a_gain(self):
        self.plan()
        sentence = self.hero()
        self.assertRegex(sentence, r'^That is \$[\d,]+\.\d{2} more than you have invested\.$')

    def test_hero_sentence_mentions_money_already_withdrawn(self):
        inv = self.plan()
        Transaction.objects.create(investment=inv, type='withdrawal', amount=Decimal('1500'), date=date.today() - timedelta(days=30))
        self.assertRegex(self.hero(), r'^Together with the \$1,500\.00 you have already withdrawn, that is \$[\d,]+\.\d{2} more than you have invested\.$')

    def test_hero_sentence_for_a_loss_has_no_minus_sign(self):
        inv = self.plan()
        ValuationSnapshot.objects.create(investment=inv, date=date.today() - timedelta(days=5), value=Decimal('8000.00'))
        sentence = self.hero()
        self.assertRegex(sentence, r'^That is \$2,000\.00 less than you have invested\.$')
        self.assertNotIn('-', sentence)

    def test_hero_sentence_at_break_even(self):
        inv = self.plan(days_ago=0)                                         # started today: worth exactly what was invested
        self.assertEqual(self.hero(), 'That is exactly what you have invested.')

    def test_the_gain_and_loss_colours_follow_the_sign(self):
        inv = self.plan()
        html = self.client.get(reverse('investors:home')).content.decode()
        self.assertRegex(html, r'class="v up"')
        ValuationSnapshot.objects.create(investment=inv, date=date.today() - timedelta(days=5), value=Decimal('8000.00'))
        self.assertRegex(self.client.get(reverse('investors:home')).content.decode(), r'class="v down"')

    def test_no_arrows_or_middle_dot_strings_in_visible_copy(self):
        inv = self.plan()
        for url in (reverse('investors:home'), reverse('investors:investment', args=[inv.reference]),
                    reverse('investors:withdrawals'), reverse('investors:withdrawal_new', )):
            html = self.client.get(url).content.decode()
            body = re.sub(r'<(script|style).*?</\1>', '', html, flags=re.S)
            text = re.sub(r'<[^>]+>', ' ', body)
            for glyph in ('←', '→', '·'):
                self.assertNotIn(glyph, text, f'{glyph!r} found on {url}')


class FilterTests(TestCase):
    def test_money_abs_drops_the_sign_and_keeps_the_currency(self):
        self.assertEqual(money_abs(Decimal('-2000'), 'USD'), '$2,000.00')
        self.assertEqual(money_abs(Decimal('1234.5'), 'EUR'), '€1,234.50')
        self.assertEqual(money_abs(Decimal('0'), 'GBP'), '£0.00')
