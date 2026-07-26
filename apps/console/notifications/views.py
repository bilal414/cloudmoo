from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from .models import NotificationLog


class IndexView(LoginRequiredMixin, TemplateView):
    template_name = "console/notifications/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        active_account = user.member.active_account
        
        # Get page number from request
        page = self.request.GET.get('page', 1)
        try:
            page = int(page)
        except ValueError:
            page = 1

       # Extract search filters from request
        filters = {}
        if self.request.GET.get('email'):
            filters['email'] = self.request.GET.get('email').strip()
        if self.request.GET.get('provider'):
            filters['provider'] = self.request.GET.get('provider').strip()
        if self.request.GET.get('asset_type'):
            filters['asset_type'] = self.request.GET.get('asset_type').strip()
        if self.request.GET.get('date_from'):
            filters['date_from'] = self.request.GET.get('date_from').strip()
        if self.request.GET.get('date_to'):
            filters['date_to'] = self.request.GET.get('date_to').strip()

        # Get notification logs for the active account with filters
        logs = NotificationLog.get_logs_for_account(
            active_account.id, 
            filters=filters if filters else None, 
            limit=500  # Increased limit for better filtering
        )
        
        # Paginate the results
        paginator = Paginator(logs, 20)  # Show 20 logs per page
        page_obj = paginator.get_page(page)
        
        context.update({
            'active_account': active_account,
            'logs': page_obj,
            'total_logs': len(logs),
            'applied_filters': filters,
            'active_url': 'notifications',
        })
        
        return context
