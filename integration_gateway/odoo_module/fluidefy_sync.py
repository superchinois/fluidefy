import base64
import psycopg2
from odoo import models, fields, api
from app.config import settings

class FluidefySyncCron(models.TransientModel):
    _name = 'fluidefy.sync.cron'
    _description = 'Fluidefy Intermediate Storage Synchronization Agent'

    def sync_inbound_xml_orders(self):
        conn = psycopg2.connect(settings.DATABASE_URL)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT id, file_name, file_base64 FROM staging_orders WHERE processing_status = 'pending';"
        )
        records = cursor.fetchall()

        for rec_id, name, base64_data in records:
            try:
                xml_content = base64.b64decode(base64_data)
                # self.env['sale.order'].parse_and_create_from_xml(xml_content)
                
                cursor.execute(
                    "UPDATE staging_orders SET processing_status = 'integrated' WHERE id = %s;", 
                    (rec_id,)
                )
            except Exception as e:
                conn.rollback()
                cursor.execute(
                    "UPDATE staging_orders SET processing_status = 'failed', error_log = %s WHERE id = %s;", 
                    (str(e), rec_id)
                )
        
        conn.commit()
        cursor.close()
        conn.close()