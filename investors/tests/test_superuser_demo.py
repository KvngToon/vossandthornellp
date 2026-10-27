import os
import shutil
import tempfile
from datetime import date
from decimal import Decimal
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from investors import crypto
from investors.demo_data import DemoSeeder
from investors.models import (Document, Investment, InvestorProfile, PayoutAccount, Transaction, ValuationSnapshot,
                              WithdrawalRequest)

PW = 'correct-horse-battery-9'
KEY = Fernet.generate_key().decode()
CHANGELIST = 'admin:investors_investorprofile_changelist'
CONFIRM_FORM = 'name="post" value="yes"'   # only rendered when Django will actually allow the deletion
BLOCKED_TEXT = "doesn't have permission to delete"


@override_settings(FIELD_ENCRYPTION_KEY=KEY)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        field = Document._meta.get_field('file')
        keep, field.storage = field.storage, FileSystemStorage(location=tmp)
        self.addCleanup(setattr, field, 'storage', keep)

        # An admin created exactly the way you would from the terminal: manage.py createsuperuser
        with mock.patch.dict(os.environ, {'DJANGO_SUPERUSER_PASSWORD': PW}):
            call_command('createsuperuser', interactive=False, username='terminal-admin', email='admin@corp.com', verbosity=0)
        self.admin = User.objects.get(username='terminal-admin')
        self.client.force_login(self.admin)

        DemoSeeder().run()
        self.demo_profile = InvestorProfile.objects.get(user__username='demo')
        self.demo_wr = WithdrawalRequest.objects.filter(investor=self.demo_profile, status='declined').latest('submitted_at')  # the recent one

        # a real client with a real withdrawal request
        user = User.objects.create_user('realclient', 'real@client.com', 'their-pass-123')
        self.real_profile = InvestorProfile.objects.create(user=user, display_name='Real Client')
        self.real_inv = Investment.objects.create(
            investor=self.real_profile, plan_name='Real Plan', principal=Decimal('75000'), annual_rate=Decimal('9'),
            start_date=date(2024, 1, 1), maturity_date=date(2028, 1, 1))
        self.real_wr = WithdrawalRequest.objects.create(
            investor=self.real_profile, investment=self.real_inv, amount=Decimal('1000'), currency='USD',
            destination_type='offline', destination_label='To be arranged with staff')

    def change_url(self, wr):
        return reverse('admin:investors_withdrawalrequest_change', args=[wr.pk])

    def clerk(self):
        user = User.objects.create_user('clerk', 'clerk@corp.com', PW, is_staff=True)
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label='investors'))
        return user


class TerminalSuperuserTests(Base):
    def test_createsuperuser_account_has_every_permission(self):
        self.assertTrue(self.admin.is_staff and self.admin.is_superuser and self.admin.is_active)
        for perm in Permission.objects.filter(content_type__app_label='investors'):
            self.assertTrue(self.admin.has_perm(f'investors.{perm.codename}'), perm.codename)  # incl. reveal_bank_details

    def test_every_investor_admin_page_opens(self):
        for name in ('investorprofile', 'investment', 'document', 'payoutaccount', 'withdrawalrequest', 'notificationlog'):
            self.assertEqual(self.client.get(reverse(f'admin:investors_{name}_changelist')).status_code, 200, name)


class DemoWithdrawalFullEditTests(Base):
    def edit_data(self, **overrides):
        data = {
            'status': 'completed', 'amount': '123.45', 'currency': 'USD', 'destination_type': 'typed',
            'destination_label': 'Edited label', 'holder_name': 'Edited Holder', 'bank_name': 'Edited Bank',
            'decline_reason': '', 'staff_note': 'edited by the superuser', 'edit_detected_country': 'gb',
            'edit_submitted_at_0': '2026-01-05', 'edit_submitted_at_1': '10:30:00', 'edit_decided_at_0': '', 'edit_decided_at_1': '',
            'account_number': '555566667777', 'routing_number': '021000021',
        }
        data.update(overrides)
        return data

    def test_form_exposes_every_field_for_demo_requests(self):
        page = self.client.get(self.change_url(self.demo_wr)).content.decode()
        for field in ('status', 'amount', 'currency', 'destination_type', 'destination_label', 'holder_name', 'bank_name',
                      'callback_verified', 'decline_reason', 'staff_note', 'edit_submitted_at_0', 'account_number', 'routing_number'):
            self.assertIn(f'name="{field}"', page, field)
        self.assertIn('everything is editable', page)

    def test_superuser_can_make_changes_the_real_workflow_forbids(self):
        # declined is a dead end for real requests; typed + completed without a call-back is also forbidden for real ones
        with mock.patch('investors.emails._send') as send, self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(self.change_url(self.demo_wr), self.edit_data())
        self.assertEqual(r.status_code, 302)
        wr = WithdrawalRequest.objects.get(pk=self.demo_wr.pk)
        self.assertEqual((wr.status, wr.amount, wr.destination_label, wr.detected_country), ('completed', Decimal('123.45'), 'Edited label', 'GB'))
        self.assertFalse(wr.callback_verified)
        self.assertEqual(wr.submitted_at.date(), date(2026, 1, 5))
        self.assertEqual(crypto.decrypt(wr.account_number_enc), '555566667777')
        self.assertEqual(wr.account_last4, '7777')
        send.assert_not_called()

    def test_blank_numbers_keep_the_stored_ones_and_invalid_ones_are_rejected(self):
        typed = WithdrawalRequest.objects.get(investor=self.demo_profile, status='under_review')
        before = (crypto.decrypt(typed.account_number_enc), crypto.decrypt(typed.routing_number_enc))
        self.client.post(self.change_url(typed), self.edit_data(status='under_review', account_number='', routing_number=''))
        typed.refresh_from_db()
        self.assertEqual((crypto.decrypt(typed.account_number_enc), crypto.decrypt(typed.routing_number_enc)), before)
        self.assertEqual(self.client.post(self.change_url(typed), self.edit_data(routing_number='021000022')).status_code, 200)
        self.assertEqual(self.client.post(self.change_url(typed), self.edit_data(account_number='12')).status_code, 200)

    def test_real_request_stays_fully_restricted_even_for_the_superuser(self):
        page = self.client.get(self.change_url(self.real_wr)).content.decode()
        self.assertNotIn('name="amount"', page)
        self.assertNotIn('name="edit_submitted_at_0"', page)
        r = self.client.post(self.change_url(self.real_wr), {'status': 'completed', 'decline_reason': '', 'staff_note': ''})
        self.assertEqual(r.status_code, 200)
        self.real_wr.refresh_from_db()
        self.assertEqual(self.real_wr.status, 'submitted')
        r = self.client.post(self.change_url(self.real_wr), {'status': 'under_review', 'amount': '1', 'decline_reason': '', 'staff_note': ''})
        self.real_wr.refresh_from_db()
        self.assertEqual(self.real_wr.amount, Decimal('1000.00'))  # an injected amount is ignored

    def test_ordinary_staff_get_the_restricted_form_on_demo_requests_too(self):
        self.client.force_login(self.clerk())
        page = self.client.get(self.change_url(self.demo_wr)).content.decode()
        self.assertNotIn('name="amount"', page)
        self.assertNotIn('everything is editable', page)


class DemoDeleteTests(Base):
    def test_superuser_can_delete_a_demo_withdrawal_but_not_a_real_one(self):
        url = reverse('admin:investors_withdrawalrequest_delete', args=[self.demo_wr.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {'post': 'yes'})
        self.assertFalse(WithdrawalRequest.objects.filter(pk=self.demo_wr.pk).exists())
        real_url = reverse('admin:investors_withdrawalrequest_delete', args=[self.real_wr.pk])
        self.assertEqual(self.client.get(real_url).status_code, 403)
        self.assertEqual(self.client.post(real_url, {'post': 'yes'}).status_code, 403)
        self.assertTrue(WithdrawalRequest.objects.filter(pk=self.real_wr.pk).exists())

    def test_ordinary_staff_cannot_delete_demo_withdrawals(self):
        self.client.force_login(self.clerk())
        self.assertEqual(self.client.get(reverse('admin:investors_withdrawalrequest_delete', args=[self.demo_wr.pk])).status_code, 403)

    def test_bulk_action_deletes_only_demo_requests(self):
        ids = [self.demo_wr.pk, self.real_wr.pk]
        r = self.client.post(reverse('admin:investors_withdrawalrequest_changelist'),
                             {'action': 'delete_demo_requests', '_selected_action': ids}, follow=True)
        self.assertContains(r, '1 real request(s) were left untouched')
        self.assertFalse(WithdrawalRequest.objects.filter(pk=self.demo_wr.pk).exists())
        self.assertTrue(WithdrawalRequest.objects.filter(pk=self.real_wr.pk).exists())

    def test_bulk_action_is_not_offered_to_ordinary_staff(self):
        self.client.force_login(self.clerk())
        self.assertNotContains(self.client.get(reverse('admin:investors_withdrawalrequest_changelist')), 'delete_demo_requests')
        r = self.client.post(reverse('admin:investors_withdrawalrequest_changelist'),
                             {'action': 'delete_demo_requests', '_selected_action': [self.demo_wr.pk]})
        self.assertTrue(WithdrawalRequest.objects.filter(pk=self.demo_wr.pk).exists())

    def test_demo_investment_with_withdrawals_can_be_deleted_by_superuser(self):
        inv = Investment.objects.get(investor=self.demo_profile, plan_name='Heritage Growth Plan')
        self.assertTrue(inv.withdrawals.exists())
        url = reverse('admin:investors_investment_delete', args=[inv.pk])
        self.assertContains(self.client.get(url), CONFIRM_FORM)
        self.client.post(url, {'post': 'yes'})
        self.assertFalse(Investment.objects.filter(pk=inv.pk).exists())
        self.assertFalse(WithdrawalRequest.objects.filter(investment_id=inv.pk).exists())
        self.assertTrue(Investment.objects.filter(investor=self.demo_profile).exists())  # its siblings survive

    def test_real_investment_with_withdrawals_stays_protected_for_superuser(self):
        url = reverse('admin:investors_investment_delete', args=[self.real_inv.pk])
        page = self.client.get(url)
        self.assertContains(page, BLOCKED_TEXT)
        self.assertNotContains(page, CONFIRM_FORM)
        self.client.post(url, {'post': 'yes'})
        self.assertTrue(Investment.objects.filter(pk=self.real_inv.pk).exists())
        self.assertTrue(WithdrawalRequest.objects.filter(pk=self.real_wr.pk).exists())

    def test_whole_demo_investor_can_be_deleted_with_everything(self):
        url = reverse('admin:investors_investorprofile_delete', args=[self.demo_profile.pk])
        self.assertContains(self.client.get(url), CONFIRM_FORM)
        self.client.post(url, {'post': 'yes'})
        self.assertFalse(InvestorProfile.objects.filter(pk=self.demo_profile.pk).exists())
        for model in (Investment, WithdrawalRequest, Document, PayoutAccount):
            self.assertFalse(model.objects.filter(**{'investor' if model is not Investment else 'investor': self.demo_profile.pk}).exists())

    def test_real_investor_with_investments_cannot_be_deleted(self):
        url = reverse('admin:investors_investorprofile_delete', args=[self.real_profile.pk])
        page = self.client.get(url)
        self.assertContains(page, BLOCKED_TEXT)
        self.assertNotContains(page, CONFIRM_FORM)
        self.client.post(url, {'post': 'yes'})
        self.assertTrue(InvestorProfile.objects.filter(pk=self.real_profile.pk).exists())

    def test_bulk_delete_mixing_demo_and_real_deletes_nothing(self):
        r = self.client.post(reverse(CHANGELIST), {
            'action': 'delete_selected', 'post': 'yes', '_selected_action': [self.demo_profile.pk, self.real_profile.pk]})
        self.assertTrue(InvestorProfile.objects.filter(pk=self.demo_profile.pk).exists())
        self.assertTrue(InvestorProfile.objects.filter(pk=self.real_profile.pk).exists())

    def test_ordinary_staff_still_cannot_delete_demo_investors_with_data(self):
        self.client.force_login(self.clerk())
        url = reverse('admin:investors_investorprofile_delete', args=[self.demo_profile.pk])
        self.client.post(url, {'post': 'yes'})
        self.assertTrue(InvestorProfile.objects.filter(pk=self.demo_profile.pk).exists())


class DemoDataEditTests(Base):
    def test_superuser_can_edit_a_demo_investment_and_its_ledger(self):
        inv = Investment.objects.get(investor=self.demo_profile, plan_name='Income Plan')
        data = {
            'investor': self.demo_profile.pk, 'plan_name': 'Edited Plan', 'principal': '99999.00', 'currency': 'USD',
            'annual_rate': '12.5', 'compounding': 'quarterly', 'payout_mode': 'maturity',
            'start_date': inv.start_date.isoformat(), 'maturity_date': inv.maturity_date.isoformat(), 'status': 'closed',
            'created_at_0': '2026-01-01', 'created_at_1': '00:00:00',
        }
        for prefix in ('transactions', 'snapshots'):
            data.update({f'{prefix}-TOTAL_FORMS': '0', f'{prefix}-INITIAL_FORMS': '0',
                         f'{prefix}-MIN_NUM_FORMS': '0', f'{prefix}-MAX_NUM_FORMS': '1000'})
        r = self.client.post(reverse('admin:investors_investment_change', args=[inv.pk]), data)
        self.assertEqual(r.status_code, 302)
        inv.refresh_from_db()
        self.assertEqual((inv.plan_name, inv.principal, inv.annual_rate, inv.status), ('Edited Plan', Decimal('99999.00'), Decimal('12.500'), 'closed'))

    def test_superuser_can_edit_a_demo_investor_profile(self):
        r = self.client.post(reverse('admin:investors_investorprofile_change', args=[self.demo_profile.pk]), {
            'display_name': 'Renamed Demo', 'phone': '000', 'is_active': 'on', 'is_demo': 'on'})
        self.assertEqual(r.status_code, 302)
        self.demo_profile.refresh_from_db()
        self.assertEqual(self.demo_profile.display_name, 'Renamed Demo')
        self.assertTrue(self.demo_profile.is_demo)


class DemoFlagToggleTests(Base):
    """Superusers can switch demo mode on and off at will; everything that depends on it follows."""

    def post_profile(self, profile, demo):
        data = {'display_name': profile.display_name, 'phone': '', 'is_active': 'on'}
        if demo:
            data['is_demo'] = 'on'
        return self.client.post(reverse('admin:investors_investorprofile_change', args=[profile.pk]), data)

    def test_checkbox_is_editable_and_explains_the_consequences(self):
        page = self.client.get(reverse('admin:investors_investorprofile_change', args=[self.demo_profile.pk])).content.decode()
        self.assertIn('name="is_demo"', page)
        self.assertIn('NEVER tick this for a real client', page)

    def test_superuser_can_untick_and_tick_again(self):
        self.assertEqual(self.post_profile(self.demo_profile, demo=False).status_code, 302)
        self.demo_profile.refresh_from_db()
        self.assertFalse(self.demo_profile.is_demo)
        self.post_profile(self.demo_profile, demo=True)
        self.demo_profile.refresh_from_db()
        self.assertTrue(self.demo_profile.is_demo)

    def test_changes_are_recorded_in_the_admin_history(self):
        self.post_profile(self.demo_profile, demo=False)
        entry = LogEntry.objects.filter(user=self.admin, object_id=str(self.demo_profile.pk)).latest('id')
        self.assertIn('is demo', entry.change_message.lower())

    def test_unticking_hands_the_account_back_to_the_real_client_rules(self):
        self.post_profile(self.demo_profile, demo=False)
        page = self.client.get(self.change_url(self.demo_wr)).content.decode()
        self.assertNotIn('everything is editable', page)                      # restricted form again
        self.assertEqual(self.client.get(reverse('admin:investors_withdrawalrequest_delete', args=[self.demo_wr.pk])).status_code, 403)
        self.open_as_status(self.demo_profile)                                 # and it can no longer be impersonated

    def open_as_status(self, profile):
        r = self.client.post(reverse(CHANGELIST), {'action': 'sign_in_as_demo_investor', '_selected_action': [profile.pk]}, follow=True)
        self.assertContains(r, 'Only active demo investors')
        self.assertEqual(int(self.client.session['_auth_user_id']), self.admin.pk)

    def test_unticking_resumes_emails_and_ticking_stops_them_again(self):
        from investors.notifications import notify
        with mock.patch('investors.emails._send', return_value=True) as send:
            self.assertFalse(notify(InvestorProfile.objects.get(pk=self.demo_profile.pk), 'valuation'))
            self.post_profile(self.demo_profile, demo=False)
            self.assertTrue(notify(InvestorProfile.objects.get(pk=self.demo_profile.pk), 'valuation'))
            self.post_profile(self.demo_profile, demo=True)
            self.assertFalse(notify(InvestorProfile.objects.get(pk=self.demo_profile.pk), 'document'))
        self.assertEqual(send.call_count, 1)

    def test_ticking_a_real_client_switches_on_the_demo_powers_for_that_account(self):
        self.post_profile(self.real_profile, demo=True)
        self.real_profile.refresh_from_db()
        self.assertTrue(self.real_profile.is_demo)
        self.assertIn('everything is editable', self.client.get(self.change_url(self.real_wr)).content.decode())

    def test_ordinary_staff_cannot_toggle_it_even_by_forging_the_field(self):
        self.client.force_login(self.clerk())
        page = self.client.get(reverse('admin:investors_investorprofile_change', args=[self.real_profile.pk])).content.decode()
        self.assertNotIn('name="is_demo"', page)
        self.post_profile(self.real_profile, demo=True)       # forged: the field is read-only for them
        self.real_profile.refresh_from_db()
        self.assertFalse(self.real_profile.is_demo)
        self.post_profile(self.demo_profile, demo=False)
        self.demo_profile.refresh_from_db()
        self.assertTrue(self.demo_profile.is_demo)

    def test_the_remove_demo_data_button_still_never_touches_a_toggled_real_client(self):
        self.post_profile(self.real_profile, demo=True)       # flagged, but not a demo username / example.com email
        DemoSeeder().remove()
        self.assertTrue(InvestorProfile.objects.filter(pk=self.real_profile.pk).exists())
        self.assertTrue(Investment.objects.filter(pk=self.real_inv.pk).exists())

    def test_new_investors_added_in_the_admin_are_never_demo(self):
        with mock.patch('investors.admin.send_invite_email', return_value=True):
            self.client.post(reverse('admin:investors_investorprofile_add'), {
                'display_name': 'New Client', 'username': 'new.client', 'email': 'new@client.com', 'phone': '', 'is_active': 'on'})
        self.assertFalse(InvestorProfile.objects.get(user__username='new.client').is_demo)


class SignInAsDemoTests(Base):
    def open_as(self, profile):
        return self.client.post(reverse(CHANGELIST), {'action': 'sign_in_as_demo_investor', '_selected_action': [profile.pk]})

    def test_superuser_steps_into_a_demo_portal_and_back_out(self):
        elena = InvestorProfile.objects.get(user__username='demo-elena')
        r = self.open_as(elena)
        self.assertRedirects(r, reverse('investors:home'), fetch_redirect_response=False)
        self.assertEqual(self.client.session['impersonator_id'], self.admin.pk)
        page = self.client.get(reverse('investors:home'))
        self.assertContains(page, 'Founders Circle Note')
        self.assertContains(page, 'Return to admin')
        self.assertEqual(self.client.get('/admin/').status_code, 302)  # now just a demo investor: not staff

        back = self.client.post(reverse('investors:return_to_admin'))
        self.assertRedirects(back, reverse(CHANGELIST), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse(CHANGELIST)).status_code, 200)
        self.assertNotIn('impersonator_id', self.client.session)
        self.assertTrue(LogEntry.objects.filter(user=self.admin, change_message__contains='Opened the portal as this demo investor').exists())

    def test_works_for_the_forced_password_change_account_too(self):
        chidi = InvestorProfile.objects.get(user__username='demo-chidi')
        self.open_as(chidi)
        page = self.client.get(reverse('investors:home'), follow=True)
        self.assertContains(page, 'Choose your password')
        self.assertContains(page, 'Return to admin')

    def test_real_clients_can_never_be_opened(self):
        r = self.client.post(reverse(CHANGELIST), {'action': 'sign_in_as_demo_investor', '_selected_action': [self.real_profile.pk]}, follow=True)
        self.assertContains(r, 'Only active demo investors')
        self.assertEqual(int(self.client.session['_auth_user_id']), self.admin.pk)  # still the admin

    def test_exactly_one_investor_must_be_selected(self):
        ids = [self.demo_profile.pk, InvestorProfile.objects.get(user__username='demo-elena').pk]
        r = self.client.post(reverse(CHANGELIST), {'action': 'sign_in_as_demo_investor', '_selected_action': ids}, follow=True)
        self.assertContains(r, 'Select exactly one')
        self.assertEqual(int(self.client.session['_auth_user_id']), self.admin.pk)

    def test_ordinary_staff_cannot_use_it(self):
        clerk = self.clerk()
        self.client.force_login(clerk)
        self.open_as(self.demo_profile)
        self.assertEqual(int(self.client.session['_auth_user_id']), clerk.pk)
        self.assertNotIn('impersonator_id', self.client.session)

    def test_no_return_button_for_ordinary_demo_sign_ins_or_real_clients(self):
        self.client.logout()
        self.demo_profile.user.set_password('a-known-pass-123')
        self.demo_profile.user.save()
        self.client.post(reverse('investors:login'), {'username': 'demo', 'password': 'a-known-pass-123'})
        self.assertNotContains(self.client.get(reverse('investors:home')), 'Return to admin')
        r = self.client.post(reverse('investors:return_to_admin'))   # no impersonation behind this session
        self.assertRedirects(r, reverse('investors:login'), fetch_redirect_response=False)
        self.assertNotIn('_auth_user_id', self.client.session)       # and it signed them out

    def test_return_endpoint_is_useless_to_anyone_else(self):
        self.assertEqual(self.client.get(reverse('investors:return_to_admin')).status_code, 405)
        self.client.logout()
        self.client.post(reverse('investors:login'), {'username': 'realclient', 'password': 'their-pass-123'})
        r = self.client.post(reverse('investors:return_to_admin'))
        self.assertRedirects(r, reverse('investors:login'), fetch_redirect_response=False)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_a_planted_session_marker_cannot_escalate(self):
        # marker names a non-superuser staff member
        clerk = self.clerk()
        self.client.logout()
        self.demo_profile.user.set_password('a-known-pass-123')
        self.demo_profile.user.save()
        self.client.post(reverse('investors:login'), {'username': 'demo', 'password': 'a-known-pass-123'})
        session = self.client.session
        session['impersonator_id'] = clerk.pk
        session.save()
        self.client.post(reverse('investors:return_to_admin'))
        self.assertNotIn('_auth_user_id', self.client.session)
        # marker names a deactivated superuser
        self.client.post(reverse('investors:login'), {'username': 'demo', 'password': 'a-known-pass-123'})
        self.admin.is_active = False
        self.admin.save()
        session = self.client.session
        session['impersonator_id'] = self.admin.pk
        session.save()
        self.client.post(reverse('investors:return_to_admin'))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_marker_does_not_survive_signing_out(self):
        self.open_as(self.demo_profile)
        self.client.post(reverse('investors:logout'))
        self.assertNotIn('impersonator_id', self.client.session)
