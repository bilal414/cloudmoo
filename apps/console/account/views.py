from django.views.generic import ListView
from django.views.generic.edit import UpdateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from .models import CoreAccount
from ..member.models import CoreMember


class AccountListView(LoginRequiredMixin, ListView):
    template_name = 'console/account/list.html'
    context_object_name = 'accounts'

    def get_queryset(self):
        return self.request.user.member.accounts.all()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_account'] = self.request.user.member.active_account
        return context


class SwitchAccountView(LoginRequiredMixin, UpdateView):
    http_method_names = ['post']
    model = CoreMember
    fields = ['active_account']
    success_url = reverse_lazy('account:list')  # Updated URL name

    def post(self, request, *args, **kwargs):
        account_id = self.kwargs.get('account_id')
        account = get_object_or_404(CoreAccount, id=account_id)
        member = self.request.user.member

        if member.set_active_account(account):
            self.request.session['active_account_id'] = account.id
            return JsonResponse({'success': True, 'account_name': account.name})
        else:
            return JsonResponse({'success': False, 'error': 'You do not have access to this account'}, status=403)

    def get_object(self, queryset=None):
        return self.request.user.member
