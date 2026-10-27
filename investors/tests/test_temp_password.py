import re
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from investors.models import InvestorProfile
from investors.passwords import generate_temporary_password

PW = 'correct-horse-battery-9'
NEW_PW = 'My-Own-Passphrase-2026'


class GeneratorTests(TestCase):
    def test_format_and_uniqueness(self):
        seen = {generate_temporary_password() for _ in range(200)}
        self.assertEqual(len(seen), 200)
        for pw in seen:
            self.assertRegex(pw, r'^[a-km-zA-HJ-NP-Z2-9]{4}(-[a-km-zA-HJ-NP-Z2-9]{4}){2}$')  # no look-alike characters
            self.assertGreaterEqual(len(pw), 12)


class TemporaryPasswordFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        admin = User.objects.create_superuser('root', 'root@example.com', PW)
        self.client.force_login(admin)
        with mock.patch('investors.admin.send_invite_email', return_value=False) as send:  # email "fails"
            r = self.client.post(reverse('admin:investors_investorprofile_add'), {
                'display_name': 'Tess', 'username': 'tess', 'email': 'tess@example.com', 'phone': '', 'is_active': 'on'},
                follow=True)
        self.temp = send.call_args.args[2]
        self.admin_page = r.content.decode()
        self.client.logout()

    def test_password_is_shown_to_staff_even_when_email_fails(self):
        self.assertIn(self.temp, self.admin_page)
        self.assertIn('could NOT be sent', self.admin_page)

    def test_password_is_stored_hashed_only(self):
        user = User.objects.get(username='tess')
        self.assertNotIn(self.temp, user.password)
        self.assertTrue(user.check_password(self.temp))

    def sign_in(self, password=None):
        return self.client.post(reverse('investors:login'), {'username': 'tess', 'password': password or self.temp})

    def test_every_portal_page_forces_the_change_first(self):
        self.sign_in()
        for name in ('home', 'performance', 'documents', 'withdrawals', 'withdrawal_new'):
            r = self.client.get(reverse(f'investors:{name}'))
            self.assertRedirects(r, reverse('investors:change_password'), fetch_redirect_response=False, msg_prefix=name)

    def test_change_page_itself_is_reachable_and_labelled(self):
        self.sign_in()
        r = self.client.get(reverse('investors:change_password'))
        self.assertContains(r, 'Choose your password')
        self.assertContains(r, 'Temporary password')

    def test_changing_password_clears_the_flag_and_keeps_session(self):
        self.sign_in()
        r = self.client.post(reverse('investors:change_password'), {
            'old_password': self.temp, 'new_password1': NEW_PW, 'new_password2': NEW_PW})
        self.assertRedirects(r, reverse('investors:home'))
        self.assertFalse(InvestorProfile.objects.get(user__username='tess').must_change_password)
        self.assertEqual(self.client.get(reverse('investors:home')).status_code, 200)  # still signed in
        self.client.logout()
        self.assertEqual(self.sign_in(self.temp).status_code, 200)  # temporary password is dead
        self.assertEqual(self.sign_in(NEW_PW).status_code, 302)

    def test_new_password_must_differ_and_pass_validators(self):
        self.sign_in()
        url = reverse('investors:change_password')
        r = self.client.post(url, {'old_password': self.temp, 'new_password1': self.temp, 'new_password2': self.temp})
        self.assertContains(r, 'must be different')
        r = self.client.post(url, {'old_password': self.temp, 'new_password1': 'short1', 'new_password2': 'short1'})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(InvestorProfile.objects.get(user__username='tess').must_change_password)

    def test_wrong_current_password_is_throttled(self):
        self.sign_in()
        url = reverse('investors:change_password')
        for _ in range(5):
            self.client.post(url, {'old_password': 'nope', 'new_password1': NEW_PW, 'new_password2': NEW_PW})
        r = self.client.post(url, {'old_password': self.temp, 'new_password1': NEW_PW, 'new_password2': NEW_PW})
        self.assertContains(r, 'Too many failed attempts')
        self.assertTrue(InvestorProfile.objects.get(user__username='tess').must_change_password)

    def test_reset_link_also_clears_the_flag(self):
        from investors.emails import set_password_url
        user = User.objects.get(username='tess')
        path = set_password_url(user).replace('https://vossandthornellp.org', '')
        final = self.client.get(path, follow=True).redirect_chain[-1][0]
        self.client.post(final, {'new_password1': NEW_PW, 'new_password2': NEW_PW})
        self.assertFalse(InvestorProfile.objects.get(user__username='tess').must_change_password)

    def test_resend_generates_a_fresh_password_and_signs_investor_out(self):
        self.sign_in()
        self.client.post(reverse('investors:change_password'), {
            'old_password': self.temp, 'new_password1': NEW_PW, 'new_password2': NEW_PW})
        admin = User.objects.get(username='root')
        staff = self.client.__class__()
        staff.force_login(admin)
        profile = InvestorProfile.objects.get(user__username='tess')
        with mock.patch('investors.admin.send_invite_email', return_value=True) as send:
            staff.post(reverse('admin:investors_investorprofile_changelist'), {
                'action': 'resend_invite', '_selected_action': [profile.pk]})
        fresh = send.call_args.args[2]
        self.assertNotEqual(fresh, self.temp)
        profile.refresh_from_db()
        self.assertTrue(profile.must_change_password)
        self.assertFalse(User.objects.get(username='tess').check_password(NEW_PW))
        self.assertEqual(self.client.get(reverse('investors:home')).status_code, 302)  # old session is dead

    def test_email_contains_credentials_and_login_link(self):
        from investors.emails import send_invite_email
        user = User.objects.get(username='tess')
        with mock.patch('investors.emails._send', return_value=True) as send:
            send_invite_email(user, user.investor_profile, 'Abcd-Efgh-Jkmn')
        html = send.call_args.args[2]
        self.assertIn('tess', html)
        self.assertIn('Abcd-Efgh-Jkmn', html)
        self.assertIn('/investors/login/', html)
