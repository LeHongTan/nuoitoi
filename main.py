import os
import re
import shutil
import psycopg2
from datetime import datetime
from typing import List, Optional
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# 1. TẢI BIẾN MÔI TRƯỜNG
load_dotenv() # Tự động tìm và load file .env trên máy Mac của bạn
DB_URL = os.getenv("DATABASE_URL")

app = FastAPI()

# --- CẤU HÌNH THƯ MỤC ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")

os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# --- DATABASE HELPER ---
def get_db_connection():
    """Tạo kết nối tới Supabase"""
    conn = psycopg2.connect(DB_URL)
    return conn

def init_db():
    """Khởi tạo bảng trên Supabase nếu chưa có"""
    conn = get_db_connection()
    c = conn.cursor()
    # PostgreSQL dùng SERIAL thay cho AUTOINCREMENT của SQLite
    c.execute('''CREATE TABLE IF NOT EXISTS transactions
                 (id SERIAL PRIMARY KEY,
                  type TEXT, amount BIGINT, description TEXT,
                  sender_name TEXT, image_path TEXT, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS feedbacks
                 (id SERIAL PRIMARY KEY,
                  name TEXT, content TEXT, created_at TEXT)''')
    conn.commit()
    c.close()
    conn.close()

# Chạy khởi tạo database khi start app
init_db()

def get_stats():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM transactions WHERE type='IN'")
    total_in = c.fetchone()[0] or 0
    c.execute("SELECT SUM(amount) FROM transactions WHERE type='OUT'")
    total_out = c.fetchone()[0] or 0
    c.close()
    conn.close()
    return total_in, total_out, (total_in - total_out)

def get_feed():
    conn = get_db_connection()
    # Dùng RealDictCursor để kết quả trả về giống dict như SQLiteRow
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM transactions WHERE type='OUT' ORDER BY id DESC")
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows

# --- ROUTES ---
@app.get("/")
async def home(request: Request):
    total_in, total_out, balance = get_stats()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "total_in": "{:,.0f}".format(total_in),
        "total_out": "{:,.0f}".format(total_out),
        "balance": "{:,.0f}".format(balance),
        "feed": get_feed()
    })

@app.get("/admin")
async def admin_page(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})

@app.get("/supporters")
async def view_supporters(request: Request):
    conn = get_db_connection()
    c = conn.cursor(cursor_factory=RealDictCursor)
    c.execute("SELECT * FROM transactions WHERE type='IN' ORDER BY id DESC LIMIT 50")
    supporters = c.fetchall()
    c.close()
    conn.close()
    return templates.TemplateResponse("supporters.html", {"request": request, "supporters": supporters})

@app.get("/guide")
async def guide_page(request: Request):
    return templates.TemplateResponse("guide.html", {"request": request})

@app.get("/feedback")
async def feedback_page(request: Request):
    return templates.TemplateResponse("feedback.html", {"request": request})

@app.post("/api/send_feedback")
async def send_feedback(name: str = Form(...), content: str = Form(...)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO feedbacks (name, content, created_at) VALUES (%s, %s, %s)",
              (name, content, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    c.close()
    conn.close()
    return RedirectResponse(url="/feedback?success=1", status_code=303)

@app.post("/api/add_expense")
async def add_expense(amount: int = Form(...), description: str = Form(...), file: UploadFile = File(...)):
    file_location = os.path.join(UPLOAD_DIR, file.filename)
    db_image_path = f"static/uploads/{file.filename}"
    with open(file_location, "wb+") as file_object:
        shutil.copyfileobj(file.file, file_object)
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO transactions (type, amount, description, image_path, created_at) VALUES (%s, %s, %s, %s, %s)",
              ('OUT', amount, description, db_image_path, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    c.close()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

# --- WEBHOOK SEPAY ---
class SePayWebhookData(BaseModel):
    id: Optional[int] = None
    transferAmount: float = None
    content: str = None
    subAccountName: Optional[str] = None
    transactionDate: str = None

@app.post("/api/sepay-webhook")
async def sepay_webhook(data: SePayWebhookData):
    if "SEVQR" not in data.content.upper():
        return {"status": "ignored", "message": "Giao dịch vãng lai"}

    clean_content = data.content
    try:
        match = re.search(r'SEVQR\s*(.*)', data.content, re.IGNORECASE)
        if match:
            clean_content = match.group(1).strip()
    except Exception as e:
        print(f"Lỗi regex: {e}")

    sender_name = data.subAccountName
    description = clean_content
    DEFAULT_MSG = "chuyen tien nuoi toi" 
    
    if not sender_name:
        if clean_content.lower().startswith(DEFAULT_MSG):
            sender_name = "Đại Gia Ẩn Danh"
            description = "Ủng hộ Admin"
        else:
            parts = clean_content.split(' ', 1)
            if len(parts) > 1:
                sender_name = parts[0]
                description = parts[1]
            elif len(parts) == 1 and parts[0]:
                sender_name = parts[0]
                description = "Ủng hộ Admin"
            else:
                sender_name = "Đại Gia Ẩn Danh"

    conn = get_db_connection()
    c = conn.cursor()
    # PostgreSQL dùng %s làm placeholder thay cho ? của SQLite
    c.execute(
        "INSERT INTO transactions (type, amount, description, sender_name, created_at) VALUES (%s, %s, %s, %s, %s)",
        ('IN', data.transferAmount, description, sender_name, data.transactionDate)
    )
    conn.commit()
    c.close()
    conn.close()
    
    return {"status": "success", "message": "Đã lưu vào Supabase"}