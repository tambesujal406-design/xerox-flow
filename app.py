"""Xerox Flow: scan a QR, upload a PDF, pick print settings, send it to the shop queue."""
import io, math, os, socket, sqlite3, time, uuid
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from pypdf import PdfReader

SHOP_NAME = os.getenv("SHOP_NAME", "Xerox Flow")
SHOP_PIN = os.getenv("SHOP_PIN", "1234")
UPI_ID = os.getenv("UPI_ID", "")  # e.g. shop@upi ; empty hides the UPI button
PORT = int(os.getenv("PORT", "8000"))
MAX_MB = 20
RATES = {  # rupees per sheet
    "bw": {"A4": 2, "A3": 5, "A5": 1, "Letter": 2, "Legal": 3},
    "colour": {"A4": 10, "A3": 20, "A5": 6, "Letter": 10, "Legal": 12},
}
UPLOADS = Path("uploads")
UPLOADS.mkdir(exist_ok=True)

db = sqlite3.connect("jobs.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, file_id TEXT, name TEXT, pages INTEGER,
  colour TEXT, size TEXT, duplex INTEGER, copies INTEGER, sheets INTEGER,
  price INTEGER, status TEXT DEFAULT 'waiting', created REAL)""")
db.commit()

app = FastAPI(title="Xerox Flow")


def quote(pages, colour, size, duplex, copies):
    if colour not in RATES or size not in RATES[colour]:
        raise HTTPException(400, "Unknown print settings")
    copies = max(1, min(int(copies), 100))
    sheets = (math.ceil(pages / 2) if duplex else pages) * copies
    return sheets, sheets * RATES[colour][size]


def base_url():
    if os.getenv("BASE_URL"):
        return os.environ["BASE_URL"].rstrip("/")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return f"http://{ip}:{PORT}"


def shop_auth(pin: str = Query("")):
    if pin != SHOP_PIN:
        raise HTTPException(401, "Wrong PIN")


class JobIn(BaseModel):
    file_id: str
    name: str
    colour: str
    size: str
    duplex: bool
    copies: int


@app.get("/", response_class=HTMLResponse)
def customer():
    return CUSTOMER.replace("__SHOP__", SHOP_NAME).replace("__UPI__", UPI_ID)


@app.get("/shop", response_class=HTMLResponse)
def shop():
    return SHOP.replace("__SHOP__", SHOP_NAME).replace("__URL__", base_url())


@app.get("/qr.svg")
def qr():
    img = qrcode.make(base_url(), image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"File is too large. Maximum is {MAX_MB} MB.")
    try:
        pages = len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        raise HTTPException(400, "Could not read this file. Upload an unlocked PDF.")
    if pages < 1:
        raise HTTPException(400, "This PDF has no pages.")
    fid = uuid.uuid4().hex
    (UPLOADS / f"{fid}.pdf").write_bytes(data)
    return {"file_id": fid, "name": (file.filename or "document.pdf")[:80], "pages": pages}


@app.get("/api/quote")
def api_quote(pages: int, colour: str, size: str, duplex: bool, copies: int):
    sheets, price = quote(pages, colour, size, duplex, copies)
    return {"sheets": sheets, "price": price}


@app.post("/api/jobs")
def create_job(j: JobIn):
    path = UPLOADS / f"{j.file_id}.pdf"
    if not j.file_id.isalnum() or not path.exists():
        raise HTTPException(404, "Upload not found. Please choose your file again.")
    pages = len(PdfReader(str(path)).pages)
    sheets, price = quote(pages, j.colour, j.size, j.duplex, j.copies)
    cur = db.execute(
        "INSERT INTO jobs(file_id,name,pages,colour,size,duplex,copies,sheets,price,created)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (j.file_id, j.name[:80], pages, j.colour, j.size, int(j.duplex),
         max(1, min(j.copies, 100)), sheets, price, time.time()))
    db.commit()
    return {"token": cur.lastrowid, "price": price}


@app.get("/api/track/{token}")
def track(token: int):
    r = db.execute("SELECT status FROM jobs WHERE id=?", (token,)).fetchone()
    if not r:
        raise HTTPException(404, "Unknown token")
    return {"status": r["status"]}


@app.get("/api/jobs", dependencies=[Depends(shop_auth)])
def list_jobs():
    rows = db.execute("SELECT * FROM jobs WHERE status!='done' ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/jobs/{job_id}/{status}", dependencies=[Depends(shop_auth)])
def set_status(job_id: int, status: str):
    if status not in ("waiting", "printed", "done"):
        raise HTTPException(400, "Unknown status")
    db.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    db.commit()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/file", dependencies=[Depends(shop_auth)])
def job_file(job_id: int):
    r = db.execute("SELECT file_id,name FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Job not found")
    return FileResponse(UPLOADS / f"{r['file_id']}.pdf", media_type="application/pdf",
                        filename=r["name"], content_disposition_type="inline")


STATIC = Path(__file__).parent / "static"
CUSTOMER = (STATIC / "student.html").read_text(encoding="utf-8")
SHOP = (STATIC / "shop.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn
    print(f"Student page: {base_url()}   Shop page: http://localhost:{PORT}/shop")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
