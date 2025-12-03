from django.core.management.base import BaseCommand
from django_redis import get_redis_connection

class Command(BaseCommand):
    help = 'Cleans all data from Redis databases used in the project'

    def handle(self, *args, **options):
        self.stdout.write("--- CLEANING REDIS ---")
        
        # 1. Clean Default Cache (DB 1 - Tokens/Sessions)
        try:
            con_default = get_redis_connection("default")
            con_default.flushdb()
            self.stdout.write(self.style.SUCCESS("✅ Flushed 'default' Redis (DB 1)"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Failed to flush 'default': {e}"))

        # 2. Clean Ticks Cache (DB 2 - Market Data)
        try:
            con_ticks = get_redis_connection("ticks")
            con_ticks.flushdb()
            self.stdout.write(self.style.SUCCESS("✅ Flushed 'ticks' Redis (DB 2)"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Failed to flush 'ticks': {e}"))
            
        self.stdout.write("--------------------")
        self.stdout.write("Memory cleaned. You will need to Login & Restart Ticker.")