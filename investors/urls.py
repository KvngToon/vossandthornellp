from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views
from .forms import InvestorPasswordResetForm

app_name = 'investors'

urlpatterns = [
    path('', views.home, name='home'),
    path('investments/<str:reference>/', views.investment_detail, name='investment'),
    path('performance/', views.performance, name='performance'),
    path('withdrawals/', views.withdrawals_list, name='withdrawals'),
    path('withdrawals/new/', views.withdrawal_new, name='withdrawal_new'),
    path('withdrawals/<int:pk>/', views.withdrawal_detail, name='withdrawal_detail'),
    path('withdrawals/<int:pk>/cancel/', views.withdrawal_cancel, name='withdrawal_cancel'),
    path('documents/', views.documents, name='documents'),
    path('documents/<int:pk>/download/', views.document_download, name='document_download'),
    path('login/', views.InvestorLoginView.as_view(), name='login'),
    path('return-to-admin/', views.return_to_admin, name='return_to_admin'),
    path('change-password/', views.change_password, name='change_password'),
    path('logout/', auth_views.LogoutView.as_view(next_page=reverse_lazy('investors:login')), name='logout'),
    path('password-reset/', auth_views.PasswordResetView.as_view(
        template_name='investors/auth/password_reset.html',
        form_class=InvestorPasswordResetForm,
        success_url=reverse_lazy('investors:password_reset_done'),
    ), name='password_reset'),
    path('password-reset/sent/', auth_views.PasswordResetDoneView.as_view(
        template_name='investors/auth/password_reset_done.html',
    ), name='password_reset_done'),
    path('password-reset/<uidb64>/<token>/', views.InvestorPasswordResetConfirmView.as_view(
        template_name='investors/auth/password_reset_confirm.html',
        success_url=reverse_lazy('investors:password_reset_complete'),
    ), name='password_reset_confirm'),
    path('password-reset/complete/', auth_views.PasswordResetCompleteView.as_view(
        template_name='investors/auth/password_reset_complete.html',
    ), name='password_reset_complete'),
]
