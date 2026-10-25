import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@localhost:5432/db_name")
    CHANNEL_ID: str = os.getenv("CHANNEL_ID", "")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")
    CHANNEL_USERNAME: str = os.getenv("CHANNEL_USERNAME", "")
    SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "")
    
    @property
    def ADMIN_IDS(self) -> list[int]:
        admins_str = os.getenv("ADMIN_IDS", "")
        if not admins_str:
            return []
        return [int(x.strip()) for x in admins_str.split(",") if x.strip().isdigit()]

settings = Settings()
