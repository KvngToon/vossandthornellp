from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from investors.models import InvestorProfile

PW = 'correct-horse-battery-9'


class AuthTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('jane@example.com', 'jane@example.com', PW)
        self.profile = InvestorProfile.objects.create(user=self.user, display_name='Jane')
        self.login_url = reverse('investors:login')

    def login(self, email='jane@example.com', password=PW):
        return self.client.post(self.login_url, {'username': email, 'password': password})


class LoginTests(AuthTestBase):
    def test_investor_can_sign_in_and_see_home(self):
        r = self.login()
        self.assertRedirects(r, reverse('investors:home'), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse('investors:home')), 'Welcome, Jane')

    def test_email_is_case_insensitive(self):
        self.assertEqual(self.login(email='Jane@Example.com').status_code, 302)

    def test_wrong_password_and_unknown_user_give_same_error(self):
        a = self.login(password='nope')
        b = self.login(email='ghost@example.com')
        self.assertEqual(a.status_code, 200)
        self.assertEqual(a.context['form'].non_field_errors(), b.context['form'].non_field_errors())

    def test_staff_user_cannot_sign_in(self):
        staff = User.objects.create_user('boss@example.com', 'boss@example.com', PW, is_staff=True)
        InvestorProfile.objects.create(user=staff, display_name='Boss')  # bypasses clean(); still must be refused
        r = self.login(email='boss@example.com')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_user_without_profile_cannot_sign_in(self):
        User.objects.create_user('rando@example.com', 'rando@example.com', PW)
        self.login(email='rando@example.com')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_inactive_investor_cannot_sign_in(self):
        self.profile.is_active = False
        self.profile.save()
        self.login()
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_throttled_after_repeated_failures(self):
        for _ in range(5):
            self.login(password='bad')
        r = self.login()  # correct password, but locked out
        self.assertEqual(r.status_code, 429)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_logout_requires_post_and_signs_out(self):
        self.login()
        self.assertEqual(self.client.get(reverse('investors:logout')).status_code, 405)
        self.client.post(reverse('investors:logout'))
        self.assertNotIn('_auth_user_id', self.client.session)


class AccessControlTests(AuthTestBase):
    def test_anonymous_redirected_to_login(self):
        r = self.client.get(reverse('investors:home'))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r['Location'].startswith(self.login_url))

    def test_staff_gets_404_on_portal(self):
        User.objects.create_user('staff', 'staff@example.com', PW, is_staff=True)
        self.client.login(username='staff', password=PW)
        self.assertEqual(self.client.get(reverse('investors:home')).status_code, 404)

    def test_deactivated_investor_with_live_session_gets_404(self):
        self.login()
        self.profile.is_active = False
        self.profile.save()
        self.assertEqual(self.client.get(reverse('investors:home')).status_code, 404)

    def test_investor_cannot_use_admin(self):
        self.login()
        r = self.client.get('/admin/', follow=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn('/admin/login/', r['Location'])


class PasswordResetTests(AuthTestBase):
    def test_reset_sends_only_for_investors(self):
        User.objects.create_user('rando@example.com', 'rando@example.com', PW)
        with mock.patch('investors.forms.send_password_reset_email') as send:
            self.client.post(reverse('investors:password_reset'), {'email': 'rando@example.com'})
            send.assert_not_called()
            r = self.client.post(reverse('investors:password_reset'), {'email': 'jane@example.com'})
            send.assert_called_once_with(self.user)
        self.assertRedirects(r, reverse('investors:password_reset_done'))

    def test_reset_response_identical_for_unknown_email(self):
        r = self.client.post(reverse('investors:password_reset'), {'email': 'ghost@example.com'})
        self.assertRedirects(r, reverse('investors:password_reset_done'))

    def test_set_password_link_works(self):
        from investors.emails import set_password_url
        url = set_password_url(self.user)
        path = url.replace('https://vossandthornellp.org', '')
        r = self.client.get(path, follow=True)
        final = r.redirect_chain[-1][0]
        r = self.client.post(final, {'new_password1': 'Brand-new-pass-77', 'new_password2': 'Brand-new-pass-77'})
        self.assertRedirects(r, reverse('investors:password_reset_complete'))
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password('Brand-new-pass-77'))


class AdminInviteTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('root', 'root@example.com', PW)
        self.client.login(username='root', password=PW)
        self.add_url = reverse('admin:investors_investorprofile_add')

    def add(self, **overrides):
        data = {'display_name': 'New Investor', 'username': 'new.investor', 'email': 'New@Example.com',
                'phone': '', 'is_active': 'on'}
        data.update(overrides)
        return self.client.post(self.add_url, data)

    def test_adding_investor_creates_non_staff_user_with_chosen_username(self):
        with mock.patch('investors.admin.send_invite_email', return_value=True) as send:
            r = self.add(username='New.Investor')  # mixed case is normalised
        self.assertEqual(r.status_code, 302)
        user = User.objects.get(username='new.investor')
        self.assertEqual(user.email, 'new@example.com')
        self.assertFalse(user.is_staff)
        self.assertTrue(user.has_usable_password())  # a generated temporary password
        self.assertTrue(user.investor_profile.must_change_password)
        self.assertEqual(user.investor_profile.display_name, 'New Investor')
        send.assert_called_once()
        self.assertTrue(user.check_password(send.call_args.args[2]))  # the emailed password is the real one

    def test_invalid_usernames_rejected(self):
        for bad in ['ab', 'has space', 'bad/slash', '-leading', 'x' * 31, 'ünïcode']:
            r = self.add(username=bad)
            self.assertEqual(r.status_code, 200, bad)
        self.assertEqual(InvestorProfile.objects.count(), 0)

    def test_duplicate_username_rejected_case_insensitively(self):
        User.objects.create_user('Taken.Name', 'someone@example.com', PW)
        r = self.add(username='taken.name')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(InvestorProfile.objects.count(), 0)

    def test_duplicate_email_rejected(self):
        User.objects.create_user('dup', 'dup@example.com', PW)
        r = self.add(email='dup@example.com')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(InvestorProfile.objects.count(), 0)

    def test_new_investor_can_sign_in_with_username_after_setting_password(self):
        from investors.emails import set_password_url
        with mock.patch('investors.admin.send_invite_email', return_value=True):
            self.add()
        user = User.objects.get(username='new.investor')
        self.client.logout()
        path = set_password_url(user).replace('https://vossandthornellp.org', '')
        final = self.client.get(path, follow=True).redirect_chain[-1][0]
        self.client.post(final, {'new_password1': 'A-Fresh-Passphrase-88', 'new_password2': 'A-Fresh-Passphrase-88'})
        r = self.client.post(reverse('investors:login'), {'username': 'NEW.investor', 'password': 'A-Fresh-Passphrase-88'})
        self.assertRedirects(r, reverse('investors:home'), fetch_redirect_response=False)


class UsernameLoginTests(TestCase):
    def setUp(self):
        cache.clear()
        user = User.objects.create_user('jane.doe', 'jane@example.com', PW)
        InvestorProfile.objects.create(user=user, display_name='Jane')

    def login(self, username, password=PW):
        return self.client.post(reverse('investors:login'), {'username': username, 'password': password})

    def test_username_login_case_insensitive(self):
        for name in ('jane.doe', 'JANE.DOE', '  Jane.Doe '):
            self.client.logout()
            self.assertEqual(self.login(name).status_code, 302, name)

    def test_email_no_longer_works_as_login(self):
        r = self.login('jane@example.com')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_page_asks_for_username_not_email(self):
        r = self.client.get(reverse('investors:login'))
        self.assertContains(r, 'Username')
        self.assertNotContains(r, 'type="email"')

    def test_invite_and_reset_emails_state_the_username(self):
        from investors.emails import send_invite_email, send_password_reset_email
        user = User.objects.get(username='jane.doe')
        with mock.patch('investors.emails._send', return_value=True) as send:
            send_invite_email(user, user.investor_profile, 'Abcd-Efgh-Jkmn')
            send_password_reset_email(user)
        for call in send.call_args_list:
            self.assertIn('jane.doe', call.args[2])
