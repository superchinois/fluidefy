from pydantic import BaseModel

class OrderNotification(BaseModel):
    # File name as delivered by Fluidefy
    file_name: str
    # Raw file contents, base64-encoded (the watcher sends the content itself,
    # so the API/worker do not need access to the file on disk).
    file_content_base64: str

class TaskAcceptedResponse(BaseModel):
    status: str
    message: str
    task_id: str