from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://user:password@localhost:5432/fluidefy_staging"
    REDIS_URL: str = "redis://localhost:6373/0"
    ODOO_URL: str = "http://localhost:8069"
    ODOO_DB: str = "my_odoo_database"
    ODOO_USERNAME: str = "admin"
    ODOO_APIKEY: str = "your_odoo_password_or_api_key"
    # Tag id used to flag sale orders created from Fluidefy (crm.tag)
    ODOO_SO_TAGS_ID: int = 0
    # User id responsible for sale orders created from Fluidefy (res.users)
    ODOO_SO_USER_ID: int = 0
    
    class Config:
        env_file = ".env"

settings = Settings()