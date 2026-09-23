from django.contrib import admin
from tracker.models import Snapshot, DailyPnl, CrossMtmConfig, PortfolioDaily

admin.site.register(Snapshot)
admin.site.register(DailyPnl)
admin.site.register(CrossMtmConfig)
admin.site.register(PortfolioDaily)
