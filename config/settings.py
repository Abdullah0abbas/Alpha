import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class Settings:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@localhost:5432/db_name")
    CHANNEL_ID: str = os.getenv("CHANNEL_ID", "")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")
    CHANNEL_USERNAME: str = os.getenv("CHANNEL_USERNAME", "")
    SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "")

    # 7 Specialized Private Admin Channels / Supergroups (Optional; falls back to ADMIN_IDS if empty)
    ADMIN_DEPOSITS_CHANNEL_ID: str = os.getenv("ADMIN_DEPOSITS_CHANNEL_ID", "").strip()
    ADMIN_PAYOUTS_CHANNEL_ID: str = os.getenv("ADMIN_PAYOUTS_CHANNEL_ID", "").strip()
    ADMIN_API_FAILURES_CHANNEL_ID: str = os.getenv("ADMIN_API_FAILURES_CHANNEL_ID", "").strip()
    ADMIN_DISPUTES_CHANNEL_ID: str = os.getenv("ADMIN_DISPUTES_CHANNEL_ID", "").strip()
    ADMIN_ERRORS_CHANNEL_ID: str = os.getenv("ADMIN_ERRORS_CHANNEL_ID", "").strip()
    ADMIN_BACKUPS_CHANNEL_ID: str = os.getenv("ADMIN_BACKUPS_CHANNEL_ID", "").strip()
    ADMIN_NOTES_CHANNEL_ID: str = os.getenv("ADMIN_NOTES_CHANNEL_ID", "").strip()

    @property
    def ADMIN_IDS(self) -> list[int]:
        admins_str = os.getenv("ADMIN_IDS", "")
        if not admins_str:
            return []
        return [int(x.strip()) for x in admins_str.split(",") if x.strip().isdigit()]

    @property
    def REWARDS_ENABLED(self) -> bool:
        val = os.getenv("REWARDS_ENABLED", "false").strip().lower()
        return val in ("1", "true", "yes")

    @property
    def DISPLAY_UTC_OFFSET_HOURS(self) -> int:
        val = os.getenv("DISPLAY_UTC_OFFSET_HOURS", "3").strip()
        try:
            return int(val)
        except ValueError:
            return 3


settings = Settings()
