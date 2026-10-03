"""FastAPI web UI: inbox, review detail + reply, reports, sites, admin."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from . import auth
from .ai import AiUnavailable, draft_reply
from .config import settings
from .db import get_db, session_scope
from .models import (AiRule, Employee, Location, ReplyTemplate, ReportRecipient, Response as ReplyRow, Review,
                     ReviewMention, ReviewSourceLink, SavedView, SiteGroup, SyncRun, User)
from .daterange import PRESETS, DateRange, resolve_range
from .reports import (BRAND_COLORS, build_report, build_trends, employee_report, location_rank, monthly_summary,
                      render_report_html, responder_stats, window_report)
from .sources import get_adapter
from .sync import sync_all
from .text_intel import THEMES as INTEL_THEMES, apply_intel, suggest_templates

log = logging.getLogger(__name__)
HERE = Path(__file__).parent

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    from .db import init_db
    init_db()          # create tables / add new columns before serving
    yield


app = FastAPI(title="Review Manager", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))

_tz = ZoneInfo(settings.timezone)
THEMES = INTEL_THEMES
TEMPLATE_TAGS = ["general", "no_comment", "team", "employee", "amenities", "membership", "mixed", "wait", "quality",
                 "damage", "billing", "staff", "plate", "hours", "vacuums"]
ATTENTION_WINDOW_DAYS = 90


def _local(dt: Optional[datetime], fmt: str = "%b %-d, %Y %-I:%M %p") -> str:
    if not dt:
        return ""
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(_tz).strftime(fmt)


def _age(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    delta = datetime.utcnow() - dt
    h = delta.total_seconds() / 3600
    if h < 1:
        return f"{max(1, int(delta.total_seconds() // 60))}m"
    if h < 48:
        return f"{int(h)}h"
    return f"{int(h // 24)}d"


def _hours(h: Optional[float]) -> str:
    if h is None:
        return "–"
    if h < 1:
        return f"{int(h * 60)}m"
    if h < 48:
        return f"{h:.0f}h"
    return f"{h / 24:.1f}d"


templates.env.filters["local"] = _local
templates.env.filters["age"] = _age
templates.env.filters["hours"] = _hours
templates.env.globals["settings"] = settings
templates.env.globals["brand_colors"] = BRAND_COLORS
templates.env.globals["now_utc"] = datetime.utcnow
templates.env.globals["THEMES"] = THEMES
templates.env.globals["PRESETS"] = PRESETS


def _dr(range: str, start: str, end: str) -> DateRange:
    return resolve_range(range or "last30", start or "", end or "")


def _group_ids(db: Session, group_id: int) -> Optional[List[int]]:
    if not group_id:
        return None
    g = db.get(SiteGroup, group_id)
    return [l.id for l in g.locations] if g else []


def _scope(db: Session, brand: str, group_id: int, location_id: int) -> Optional[List[int]]:
    """Resolve group / site filters into a location id list (None = no restriction)."""
    ids = _group_ids(db, group_id)
    if location_id:
        ids = [location_id] if (ids is None or location_id in ids) else []
    return ids


@app.exception_handler(HTTPException)
async def _auth_redirect(request: Request, exc: HTTPException):
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
    return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>", status_code=exc.status_code)


def _attention_cond(overdue_cut: datetime):
    """Negative and unanswered, or overdue within the last 90 days."""
    recent = datetime.utcnow() - timedelta(days=ATTENTION_WINDOW_DAYS)
    return or_(Review.rating <= settings.negative_rating_max,
               (Review.created_at_source < overdue_cut) & (Review.created_at_source >= recent))


def _open_base():
    return (select(func.count()).select_from(Review).join(ReviewSourceLink)
            .where(Review.is_deleted.is_(False), ReviewSourceLink.active.is_(True),
                   Review.has_owner_reply.is_(False), Review.is_archived.is_(False)))


def _nav_counts(db: Session) -> dict:
    overdue_cut = datetime.utcnow() - timedelta(hours=settings.overdue_hours)
    return {
        "attention": db.execute(_open_base().where(_attention_cond(overdue_cut))).scalar() or 0,
        "unanswered": db.execute(_open_base()).scalar() or 0,
        "last_sync": db.execute(select(func.max(SyncRun.finished_at))).scalar(),
    }


def _render(request: Request, name: str, user: Optional[User], db: Optional[Session] = None, **ctx) -> HTMLResponse:
    ctx.update({"request": request, "user": user, "nav": _nav_counts(db) if (db is not None and user) else {}})
    # Modern Starlette signature (request first); the legacy (name, context) form was removed in Starlette 0.50.
    return templates.TemplateResponse(request, name, ctx)


def _brands(db: Session) -> List[str]:
    return sorted({l.brand for l in db.execute(select(Location)).scalars().all()})


# ----------------------------------------------------------------- auth pages
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    return _render(request, "login.html", None, next=next, error=error, sso_enabled=settings.sso_enabled,
                   password_enabled=settings.password_login_enabled, domains=settings.sso_allowed_domains)


def _session_redirect(user: User, next_url: str) -> RedirectResponse:
    resp = RedirectResponse(url=next_url or "/", status_code=303)
    resp.set_cookie(auth.COOKIE_NAME, auth.make_session_cookie(user), httponly=True, samesite="lax",
                    max_age=auth.SESSION_MAX_AGE, secure=settings.app_base_url.startswith("https"))
    return resp


@app.get("/auth/microsoft")
def microsoft_login(next: str = "/"):
    if not settings.sso_enabled:
        raise HTTPException(404, "Microsoft sign-in is not configured")
    auth_url, flow_cookie = auth.start_microsoft_flow()
    resp = RedirectResponse(url=auth_url, status_code=303)
    resp.set_cookie(auth.FLOW_COOKIE, flow_cookie, httponly=True, samesite="lax", max_age=auth.FLOW_MAX_AGE,
                    secure=settings.app_base_url.startswith("https"))
    resp.set_cookie("rm_next", next if next.startswith("/") else "/", httponly=True, samesite="lax", max_age=auth.FLOW_MAX_AGE)
    return resp


@app.get("/auth/microsoft/callback")
def microsoft_callback(request: Request, db: Session = Depends(get_db)):
    if not settings.sso_enabled:
        raise HTTPException(404, "Microsoft sign-in is not configured")
    try:
        claims = auth.finish_microsoft_flow(request.cookies.get(auth.FLOW_COOKIE), request.query_params)
        user = auth.resolve_sso_user(db, claims)
    except auth.SsoError as exc:
        log.warning("microsoft sign-in rejected: %s", exc)
        resp = RedirectResponse(url=f"/login?error={str(exc).replace(' ', '+')}", status_code=303)
        resp.delete_cookie(auth.FLOW_COOKIE)
        return resp
    next_url = request.cookies.get("rm_next") or "/"
    resp = _session_redirect(user, next_url if next_url.startswith("/") else "/")
    resp.delete_cookie(auth.FLOW_COOKIE)
    resp.delete_cookie("rm_next")
    return resp


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/"),
          db: Session = Depends(get_db)):
    user = auth.authenticate(db, email, password)
    if not user:
        return _render(request, "login.html", None, next=next, error="Wrong email or password.", sso_enabled=settings.sso_enabled,
                       password_enabled=settings.password_login_enabled, domains=settings.sso_allowed_domains)
    return _session_redirect(user, next if next.startswith("/") else "/")


@app.post("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


@app.get("/health")
def health(db: Session = Depends(get_db)):
    last = db.execute(select(func.max(SyncRun.finished_at))).scalar()
    return {"ok": True, "last_sync_finished_at": last.isoformat() if last else None}


# ----------------------------------------------------------------- inbox
VIEWS = [("attention", "Needs attention"), ("unanswered", "Unanswered"), ("negative", "Negative"),
         ("replied", "Replied"), ("all", "All"), ("failed", "Failed"), ("archived", "Archived")]
VIEW_KEYS = {k for k, _ in VIEWS}


def _inbox_query(user: User, view: str, brand: str, location_id: int, group_id: int, rating: str, q: str, dr: Optional[DateRange],
                 overdue_cut: datetime, group_location_ids: Optional[List[int]] = None):
    base = (select(Review)
            .join(ReviewSourceLink, Review.source_link_id == ReviewSourceLink.id)
            .outerjoin(Location, ReviewSourceLink.location_id == Location.id)
            .where(Review.is_deleted.is_(False), ReviewSourceLink.active.is_(True))
            .options(selectinload(Review.source_link).selectinload(ReviewSourceLink.location),
                     selectinload(Review.assigned_to), selectinload(Review.mentions),
                     selectinload(Review.responses).selectinload(ReplyRow.created_by)))
    order = (Review.created_at_source.desc(),)
    open_ = (Review.has_owner_reply.is_(False)) & (Review.is_archived.is_(False))
    if view == "attention":
        base = base.where(open_, _attention_cond(overdue_cut))
        order = ((Review.rating <= settings.negative_rating_max).desc(), Review.created_at_source.asc())
    elif view == "unanswered":
        base = base.where(open_)
        order = (Review.created_at_source.asc(),)
    elif view == "negative":
        base = base.where(Review.rating <= settings.negative_rating_max)
    elif view == "replied":
        base = base.where(Review.has_owner_reply.is_(True))
    elif view == "failed":
        failed_ids = select(ReplyRow.review_id).where(ReplyRow.status == "failed")
        base = base.where(open_, Review.id.in_(failed_ids))
    elif view == "archived":
        base = base.where(Review.is_archived.is_(True))
    if brand:
        base = base.where(Location.brand == brand)
    if location_id:
        base = base.where(Location.id == location_id)
    if group_id and group_location_ids is not None:
        base = base.where(Location.id.in_(group_location_ids or [-1]))
    if rating:
        base = base.where(Review.rating == int(rating))
    if dr is not None:
        base = base.where(Review.created_at_source >= dr.start, Review.created_at_source < dr.end)
    if q:
        like = f"%{q}%"
        base = base.where(or_(Review.text.ilike(like), Review.author_name.ilike(like)))
    return base, order


@app.get("/", response_class=HTMLResponse)
def inbox(request: Request, user: User = Depends(auth.current_user), db: Session = Depends(get_db),
          view: str = "attention", brand: str = "", location_id: int = 0, group_id: int = 0, rating: str = "", q: str = "",
          range: str = "", start: str = "", end: str = "", sv: int = 0, page: int = 1, sort: str = "", dir: str = "asc"):
    per_page = 50
    saved_views = db.execute(select(SavedView).where(or_(SavedView.is_shared.is_(True), SavedView.owner_id == user.id))
                             .order_by(SavedView.sort_order, SavedView.name)).scalars().all()
    active_view = None
    if sv:
        active_view = next((v for v in saved_views if v.id == sv), None)
        if active_view:
            p = active_view.params()
            view = p.get("view", view); brand = p.get("brand", ""); location_id = int(p.get("location_id", 0) or 0)
            group_id = int(p.get("group_id", 0) or 0); rating = str(p.get("rating", "") or ""); q = p.get("q", "")
            range = p.get("range", ""); start = p.get("start", ""); end = p.get("end", "")
            if not range and p.get("days"):
                range = {1: "yesterday", 7: "last7", 30: "last30", 90: "custom"}.get(int(p["days"]), "")
    if view not in VIEW_KEYS:
        view = "attention"
    overdue_cut = datetime.utcnow() - timedelta(hours=settings.overdue_hours)
    groups = db.execute(select(SiteGroup).options(selectinload(SiteGroup.locations)).order_by(SiteGroup.name)).scalars().all()
    group_loc_ids = None
    if group_id:
        g = next((g for g in groups if g.id == group_id), None)
        group_loc_ids = [l.id for l in g.locations] if g else []
    dr = _dr(range, start, end) if (range or start or end) else None
    base, order = _inbox_query(user, view, brand, location_id, group_id, rating, q, dr, overdue_cut, group_loc_ids)
    dir = "desc" if dir == "desc" else "asc"
    sort_cols = {
        "rating": (Review.rating,), "site": (Location.name,), "author": (Review.author_name,), "created": (Review.created_at_source,),
        "status": (Review.has_owner_reply, Review.rating, Review.created_at_source),
    }
    if sort in sort_cols:
        order = tuple((c.desc() if dir == "desc" else c.asc()) for c in sort_cols[sort]) + (Review.created_at_source.desc(),)
    else:
        sort = ""
    total = db.execute(select(func.count()).select_from(base.order_by(None).subquery())).scalar() or 0
    rows = db.execute(base.order_by(*order).offset((page - 1) * per_page).limit(per_page)).scalars().all()

    cbase = _open_base()
    all_base = (select(func.count()).select_from(Review).join(ReviewSourceLink)
                .where(Review.is_deleted.is_(False), ReviewSourceLink.active.is_(True)))
    counts = {
        "attention": db.execute(cbase.where(_attention_cond(overdue_cut))).scalar(),
        "unanswered": db.execute(cbase).scalar(),
        "negative": db.execute(all_base.where(Review.rating <= settings.negative_rating_max)).scalar(),
        "replied": db.execute(all_base.where(Review.has_owner_reply.is_(True))).scalar(),
        "failed": db.execute(cbase.where(Review.id.in_(select(ReplyRow.review_id).where(ReplyRow.status == "failed")))).scalar(),
    }
    locations = db.execute(select(Location).where(Location.active.is_(True)).order_by(Location.brand, Location.name)).scalars().all()
    current_params = {"view": view, "brand": brand, "location_id": location_id, "group_id": group_id, "rating": rating, "q": q,
                      "range": dr.preset if dr else "", "start": dr.start_date.isoformat() if (dr and dr.is_custom) else "",
                      "end": dr.end_date.isoformat() if (dr and dr.is_custom) else ""}
    qs = "&".join(f"{k}={v}" for k, v in current_params.items())
    return _render(request, "inbox.html", user, db, reviews=rows, total=total, page=page, per_page=per_page,
                   view=view, views=VIEWS, brand=brand, location_id=location_id, group_id=group_id, groups=groups, rating=rating, q=q, dr=dr,
                   counts=counts, locations=locations, brands=_brands(db), overdue_cut=overdue_cut,
                   saved_views=saved_views, active_view=active_view, qs=qs, current_params_json=json.dumps(current_params),
                   sort=sort, dir=dir)


@app.post("/groups")
def create_group(request: Request, user: User = Depends(auth.current_user), db: Session = Depends(get_db), name: str = Form(...),
                 location_ids: List[int] = Form([]), back: str = Form("/")):
    name = name.strip()[:80]
    if not name or not location_ids:
        return RedirectResponse(url=back or "/", status_code=303)
    g = db.execute(select(SiteGroup).where(SiteGroup.name == name)).scalar_one_or_none() or SiteGroup(name=name)
    db.add(g)
    g.locations = db.execute(select(Location).where(Location.id.in_(location_ids))).scalars().all()
    db.commit()
    sep = "&" if "?" in back else "?"
    return RedirectResponse(url=f"{back}{sep}group_id={g.id}" if back.startswith("/") else f"/?group_id={g.id}", status_code=303)


@app.post("/views")
def save_view(user: User = Depends(auth.current_user), db: Session = Depends(get_db), name: str = Form(...),
              is_shared: int = Form(0), params: str = Form("{}")):
    try:
        p = json.loads(params)
    except ValueError:
        p = {}
    v = SavedView(name=name.strip()[:80] or "Untitled", owner_id=user.id, is_shared=bool(is_shared), params_json=json.dumps(p))
    db.add(v)
    db.commit()
    return RedirectResponse(url=f"/?sv={v.id}", status_code=303)


@app.post("/views/{vid}/delete")
def delete_view(vid: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    v = db.get(SavedView, vid)
    if v and (v.owner_id == user.id or user.is_admin):
        db.delete(v)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


# ----------------------------------------------------------------- review detail / reply
def _get_review(db: Session, review_id: int) -> Review:
    r = db.execute(select(Review).where(Review.id == review_id)
                   .options(selectinload(Review.source_link).selectinload(ReviewSourceLink.location),
                            selectinload(Review.responses).selectinload(ReplyRow.created_by),
                            selectinload(Review.assigned_to), selectinload(Review.mentions).selectinload(ReviewMention.employee))).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Review not found")
    return r


def _neighbors(db: Session, r: Review) -> dict:
    open_ = (Review.is_deleted.is_(False)) & (ReviewSourceLink.active.is_(True)) & (Review.has_owner_reply.is_(False)) & (Review.is_archived.is_(False))
    nxt = db.execute(select(Review.id).join(ReviewSourceLink).where(open_, Review.created_at_source > r.created_at_source)
                     .order_by(Review.created_at_source.asc()).limit(1)).scalar()
    prv = db.execute(select(Review.id).join(ReviewSourceLink).where(open_, Review.created_at_source < r.created_at_source)
                     .order_by(Review.created_at_source.desc()).limit(1)).scalar()
    return {"next": nxt, "prev": prv}


def _template_suggestions(db: Session, r: Review, user: User) -> List[Dict]:
    tpls = db.execute(select(ReplyTemplate).where(ReplyTemplate.active.is_(True)).order_by(ReplyTemplate.sort_order, ReplyTemplate.name)).scalars().all()
    ranked = suggest_templates(r, tpls, r.mention_names)
    first = user.name.split()[0] if user.name else ""
    out = []
    for i, (t, score) in enumerate(ranked):
        out.append({"id": t.id, "name": t.name, "body": t.render(r, first), "score": score, "suggested": i < 3 and score > 0,
                    "band": t.rating_band, "usage": t.usage_count or 0})
    return out


@app.get("/reviews/{review_id}", response_class=HTMLResponse)
def review_detail(review_id: int, request: Request, user: User = Depends(auth.current_user),
                  db: Session = Depends(get_db), msg: str = ""):
    r = _get_review(db, review_id)
    tpls = _template_suggestions(db, r, user)
    others = db.execute(select(Review).join(ReviewSourceLink).where(
        Review.source_link_id == r.source_link_id, Review.id != r.id, Review.is_deleted.is_(False),
        Review.author_name == r.author_name, Review.author_name.isnot(None)).order_by(Review.created_at_source.desc()).limit(5)).scalars().all() if r.author_name else []
    return _render(request, "review.html", user, db, r=r, msg=msg, templates_json=json.dumps(tpls), tpls=tpls,
                   nav_links=_neighbors(db, r), same_author=others, ai_enabled=settings.ai_enabled)


@app.post("/reviews/{review_id}/reply")
def post_reply(review_id: int, text: str = Form(...), go_next: int = Form(0), template_id: int = Form(0), ai_generated: int = Form(0),
               user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    r = _get_review(db, review_id)
    text = text.strip()
    if not text:
        return RedirectResponse(url=f"/reviews/{review_id}?msg=Reply+was+empty", status_code=303)
    row = ReplyRow(review_id=r.id, text=text, created_by_id=user.id, status="draft",
                   template_id=template_id or None, ai_generated=bool(ai_generated))
    db.add(row)
    db.flush()
    try:
        posted_at = get_adapter(r.source).post_reply(r.source_link, r.external_id, text)
        row.status = "posted"
        row.posted_at = posted_at
        r.has_owner_reply = True
        r.owner_reply_text = text
        r.owner_reply_updated_at = posted_at
        r.assigned_to_id = r.assigned_to_id or user.id
        if template_id:
            t = db.get(ReplyTemplate, template_id)
            if t:
                t.usage_count = (t.usage_count or 0) + 1
                t.last_used_at = datetime.utcnow()
        msg = "Reply posted."
    except Exception as exc:
        log.exception("reply failed")
        row.status = "failed"
        row.error = str(exc)[:2000]
        msg = "Posting failed. See the error below."
    nxt = _neighbors(db, r)["next"] if (go_next and row.status == "posted") else None
    db.commit()
    if nxt:
        return RedirectResponse(url=f"/reviews/{nxt}?msg=Reply+posted.+Here+is+the+next+one.", status_code=303)
    return RedirectResponse(url=f"/reviews/{review_id}?msg={msg.replace(' ', '+')}", status_code=303)


@app.post("/reviews/{review_id}/reply/delete")
def delete_reply(review_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    r = _get_review(db, review_id)
    try:
        get_adapter(r.source).delete_reply(r.source_link, r.external_id)
        r.has_owner_reply = False
        r.owner_reply_text = None
        r.owner_reply_updated_at = None
        db.add(ReplyRow(review_id=r.id, text="(reply removed)", created_by_id=user.id, status="deleted", posted_at=datetime.utcnow()))
        msg = "Reply removed."
    except Exception as exc:
        log.exception("delete reply failed")
        db.add(ReplyRow(review_id=r.id, text="(delete attempt)", created_by_id=user.id, status="failed", error=str(exc)[:2000]))
        msg = "Delete failed."
    db.commit()
    return RedirectResponse(url=f"/reviews/{review_id}?msg={msg.replace(' ', '+')}", status_code=303)


@app.post("/reviews/{review_id}/note")
def save_note(review_id: int, note: str = Form(""), category: str = Form(""), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    r = _get_review(db, review_id)
    r.internal_note = note.strip() or None
    r.category = category.strip() or None
    db.commit()
    return RedirectResponse(url=f"/reviews/{review_id}?msg=Saved", status_code=303)


@app.post("/reviews/{review_id}/claim")
def claim(review_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db), back: str = Form("")):
    r = _get_review(db, review_id)
    if r.assigned_to_id == user.id:
        r.assigned_to_id, r.assigned_at = None, None
    else:
        r.assigned_to_id, r.assigned_at = user.id, datetime.utcnow()
    db.commit()
    return RedirectResponse(url=back or f"/reviews/{review_id}", status_code=303)


@app.post("/reviews/{review_id}/archive")
def archive(review_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db), back: str = Form("")):
    r = _get_review(db, review_id)
    r.is_archived = not r.is_archived
    r.archived_at = datetime.utcnow() if r.is_archived else None
    r.archived_by_id = user.id if r.is_archived else None
    db.commit()
    msg = "Archived.+It+will+not+count+as+unanswered." if r.is_archived else "Restored+to+the+inbox."
    return RedirectResponse(url=back or f"/reviews/{review_id}?msg={msg}", status_code=303)


@app.post("/reviews/{review_id}/mentions")
def add_mention(review_id: int, name: str = Form(...), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    r = _get_review(db, review_id)
    name = name.strip()[:80]
    if name and name not in r.mention_names:
        emp = db.execute(select(Employee).where(func.lower(Employee.name) == name.lower())).scalar_one_or_none()
        db.add(ReviewMention(review_id=r.id, name=emp.name if emp else name, employee_id=emp.id if emp else None, source="manual"))
        db.commit()
    return RedirectResponse(url=f"/reviews/{review_id}?msg=Mention+added", status_code=303)


@app.post("/reviews/{review_id}/mentions/{mid}/delete")
def delete_mention(review_id: int, mid: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    m = db.get(ReviewMention, mid)
    if m and m.review_id == review_id:
        db.delete(m)
        db.commit()
    return RedirectResponse(url=f"/reviews/{review_id}?msg=Mention+removed", status_code=303)


@app.post("/reviews/{review_id}/draft")
def draft(review_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    r = _get_review(db, review_id)
    rules = db.execute(select(AiRule).where(AiRule.active.is_(True)).order_by(AiRule.sort_order)).scalars().all()
    examples = [t for t, _ in suggest_templates(r, db.execute(select(ReplyTemplate).where(ReplyTemplate.active.is_(True))).scalars().all(), r.mention_names)][:5]
    try:
        text = draft_reply(r, rules, examples, user.name.split()[0] if user.name else "")
        return JSONResponse({"ok": True, "text": text})
    except AiUnavailable as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    except Exception as exc:  # pragma: no cover
        log.exception("draft failed")
        return JSONResponse({"ok": False, "error": "Drafting failed"}, status_code=500)


# ----------------------------------------------------------------- reports
@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request, user: User = Depends(auth.current_user), db: Session = Depends(get_db), brand: str = "", group_id: int = 0,
            range: str = "last30", start: str = "", end: str = ""):
    dr = _dr(range, start, end)
    bl = [brand] if brand else None
    ids = _scope(db, brand, group_id, 0)
    wr = window_report(db, dr, brands=bl, location_ids=ids)
    trends = build_trends(db, dr, brands=bl, location_ids=ids)
    groups = db.execute(select(SiteGroup).order_by(SiteGroup.name)).scalars().all()
    return _render(request, "reports.html", user, db, w=wr, dr=dr, trends=trends, trends_json=json.dumps(trends),
                   brand=brand, brands=_brands(db), group_id=group_id, groups=groups,
                   monthly=monthly_summary(db, bl, months=12, location_ids=ids), responders=responder_stats(db, dr),
                   rank=location_rank(db, dr, brands=bl, location_ids=ids), ai_enabled=settings.ai_enabled)


@app.get("/reports/morning", response_class=HTMLResponse)
def morning_preview(user: User = Depends(auth.current_user), db: Session = Depends(get_db), brand: str = ""):
    data = build_report(db, brands=[brand] if brand else None)
    return HTMLResponse(render_report_html(data))


@app.get("/reports/employees", response_class=HTMLResponse)
def employees_report(request: Request, user: User = Depends(auth.current_user), db: Session = Depends(get_db),
                     brand: str = "", group_id: int = 0, location_id: int = 0, range: str = "last30", start: str = "", end: str = ""):
    dr = _dr(range, start, end)
    ids = _scope(db, brand, group_id, location_id)
    rep = employee_report(db, dr, brands=[brand] if brand else None, location_ids=ids)
    locations = db.execute(select(Location).where(Location.active.is_(True)).order_by(Location.brand, Location.name)).scalars().all()
    groups = db.execute(select(SiteGroup).order_by(SiteGroup.name)).scalars().all()
    return _render(request, "reports_employees.html", user, db, rep=rep, dr=dr, brand=brand, brands=_brands(db), group_id=group_id, groups=groups,
                   location_id=location_id, locations=locations)


@app.get("/sites/{location_id}", response_class=HTMLResponse)
def site_page(location_id: int, request: Request, user: User = Depends(auth.current_user), db: Session = Depends(get_db),
              range: str = "last30", start: str = "", end: str = ""):
    loc = db.execute(select(Location).where(Location.id == location_id).options(selectinload(Location.sources))).scalar_one_or_none()
    if not loc:
        raise HTTPException(404, "Site not found")
    dr = _dr(range, start, end)
    wr = window_report(db, dr, location_ids=[loc.id])
    trends = build_trends(db, dr, location_ids=[loc.id])
    emp = employee_report(db, dr, location_ids=[loc.id])
    return _render(request, "site.html", user, db, loc=loc, w=wr, dr=dr, trends=trends, trends_json=json.dumps(trends), emp=emp)


# ----------------------------------------------------------------- locations / sync admin
@app.get("/locations", response_class=HTMLResponse)
def locations(request: Request, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    links = db.execute(select(ReviewSourceLink).options(selectinload(ReviewSourceLink.location))
                       .order_by(ReviewSourceLink.active.desc(), ReviewSourceLink.source, ReviewSourceLink.display_name)).scalars().all()
    locs = db.execute(select(Location).order_by(Location.brand, Location.name)).scalars().all()
    runs = db.execute(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(30)).scalars().all()
    return _render(request, "locations.html", user, db, links=links, locs=locs, runs=runs)


@app.post("/locations/{link_id}/link")
def link_location(link_id: int, location_id: int = Form(...), user: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    link = db.get(ReviewSourceLink, link_id)
    if not link:
        raise HTTPException(404)
    link.location_id = location_id or None
    title = (link.display_name or "").lower()
    excluded = any(pat.lower() in title for pat in settings.listing_exclude_patterns)
    link.active = bool(link.location_id) and not excluded
    db.commit()
    return RedirectResponse(url="/locations", status_code=303)


def _background_sync(full: bool) -> None:
    with session_scope() as s:
        sync_all(s, full=full)


@app.post("/sync")
def trigger_sync(background: BackgroundTasks, full: int = Form(0), user: User = Depends(auth.admin_user)):
    background.add_task(_background_sync, bool(full))
    return RedirectResponse(url="/locations", status_code=303)


# ----------------------------------------------------------------- templates
@app.get("/admin/templates", response_class=HTMLResponse)
def admin_templates(request: Request, user: User = Depends(auth.current_user), db: Session = Depends(get_db), edit: int = 0):
    tpls = db.execute(select(ReplyTemplate).options(selectinload(ReplyTemplate.updated_by))
                      .order_by(ReplyTemplate.min_rating.desc(), ReplyTemplate.sort_order, ReplyTemplate.name)).scalars().all()
    groups: Dict[str, List[ReplyTemplate]] = {}
    for t in tpls:
        groups.setdefault(t.rating_band, []).append(t)
    ordered = [(b, groups[b]) for b in ("5 star", "4 star", "1-3 star", "Any rating") if b in groups]
    editing = db.get(ReplyTemplate, edit) if edit else None
    return _render(request, "admin_templates.html", user, db, groups=ordered, brands=_brands(db), editing=editing,
                   tag_options=TEMPLATE_TAGS, total=len(tpls))


@app.post("/admin/templates")
def save_template(user: User = Depends(auth.admin_user), db: Session = Depends(get_db), id: int = Form(0),
                  name: str = Form(...), brand: str = Form(""), min_rating: int = Form(1), max_rating: int = Form(5),
                  body: str = Form(...), sort_order: int = Form(100), active: int = Form(1), tags: List[str] = Form([])):
    t = db.get(ReplyTemplate, id) if id else ReplyTemplate()
    if not id:
        db.add(t)
    t.name, t.brand, t.body = name.strip(), (brand or None), body.strip()
    t.min_rating, t.max_rating = min(min_rating, max_rating), max(min_rating, max_rating)
    t.sort_order, t.active = sort_order, bool(active)
    t.tags = ";".join(tags) or None
    t.updated_by_id = user.id
    db.commit()
    return RedirectResponse(url="/admin/templates", status_code=303)


@app.post("/admin/templates/{tid}/delete")
def delete_template(tid: int, user: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    t = db.get(ReplyTemplate, tid)
    if t:
        db.delete(t)
        db.commit()
    return RedirectResponse(url="/admin/templates", status_code=303)


# ----------------------------------------------------------------- admin: users / recipients / groups / employees / ai
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user: User = Depends(auth.admin_user), db: Session = Depends(get_db), msg: str = ""):
    users = db.execute(select(User).order_by(User.name)).scalars().all()
    return _render(request, "admin_users.html", user, db, users=users, msg=msg, sso_enabled=settings.sso_enabled,
                   domains=settings.sso_allowed_domains)


@app.post("/admin/users")
def save_user(user: User = Depends(auth.admin_user), db: Session = Depends(get_db), email: str = Form(...),
              name: str = Form(...), password: str = Form(""), role: str = Form("agent")):
    email = email.strip().lower()
    u = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if u is None:
        if not password and settings.sso_enabled and auth.email_domain_allowed(email):
            u = User(email=email, name=name.strip(), password_hash=None, role=role, auth_provider="microsoft")
        elif len(password) < 10:
            return RedirectResponse(url="/admin/users?msg=Password+must+be+at+least+10+characters+(or+leave+blank+for+a+Microsoft-only+account)", status_code=303)
        else:
            u = User(email=email, name=name.strip(), password_hash=auth.hash_password(password), role=role)
        db.add(u)
    else:
        u.name, u.role = name.strip(), role
        if password:
            if len(password) < 10:
                return RedirectResponse(url="/admin/users?msg=Password+must+be+at+least+10+characters", status_code=303)
            u.password_hash = auth.hash_password(password)
    db.commit()
    return RedirectResponse(url="/admin/users?msg=Saved", status_code=303)


@app.post("/admin/users/{uid}/toggle")
def toggle_user(uid: int, user: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if u and u.id != user.id:
        u.active = not u.active
        db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@app.get("/admin/recipients", response_class=HTMLResponse)
def admin_recipients(request: Request, user: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    rows = db.execute(select(ReportRecipient).order_by(ReportRecipient.email)).scalars().all()
    return _render(request, "admin_recipients.html", user, db, rows=rows, brands=_brands(db))


@app.post("/admin/recipients")
def save_recipient(user: User = Depends(auth.admin_user), db: Session = Depends(get_db), email: str = Form(...),
                   name: str = Form(""), brands: List[str] = Form([])):
    email = email.strip().lower()
    r = db.execute(select(ReportRecipient).where(ReportRecipient.email == email)).scalar_one_or_none()
    if r is None:
        r = ReportRecipient(email=email)
        db.add(r)
    r.name = name.strip() or None
    r.brands = ";".join(brands) or None
    r.active = True
    db.commit()
    return RedirectResponse(url="/admin/recipients", status_code=303)


@app.post("/admin/recipients/{rid}/delete")
def delete_recipient(rid: int, user: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    r = db.get(ReportRecipient, rid)
    if r:
        db.delete(r)
        db.commit()
    return RedirectResponse(url="/admin/recipients", status_code=303)


@app.get("/admin/groups", response_class=HTMLResponse)
def admin_groups(request: Request, user: User = Depends(auth.admin_user), db: Session = Depends(get_db), edit: int = 0):
    groups = db.execute(select(SiteGroup).options(selectinload(SiteGroup.locations)).order_by(SiteGroup.name)).scalars().all()
    locs = db.execute(select(Location).where(Location.active.is_(True)).order_by(Location.brand, Location.name)).scalars().all()
    editing = db.get(SiteGroup, edit) if edit else None
    return _render(request, "admin_groups.html", user, db, groups=groups, locs=locs, editing=editing)


@app.post("/admin/groups")
def save_group(user: User = Depends(auth.admin_user), db: Session = Depends(get_db), id: int = Form(0), name: str = Form(...),
               description: str = Form(""), location_ids: List[int] = Form([])):
    g = db.get(SiteGroup, id) if id else SiteGroup()
    if not id:
        db.add(g)
    g.name, g.description = name.strip()[:80], description.strip() or None
    g.locations = db.execute(select(Location).where(Location.id.in_(location_ids or [-1]))).scalars().all()
    db.commit()
    return RedirectResponse(url="/admin/groups", status_code=303)


@app.post("/admin/groups/{gid}/delete")
def delete_group(gid: int, user: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    g = db.get(SiteGroup, gid)
    if g:
        db.delete(g)
        db.commit()
    return RedirectResponse(url="/admin/groups", status_code=303)


@app.get("/admin/employees", response_class=HTMLResponse)
def admin_employees(request: Request, user: User = Depends(auth.admin_user), db: Session = Depends(get_db), edit: int = 0):
    employees = db.execute(select(Employee).options(selectinload(Employee.location)).order_by(Employee.active.desc(), Employee.name)).scalars().all()
    locs = db.execute(select(Location).where(Location.active.is_(True)).order_by(Location.brand, Location.name)).scalars().all()
    since = datetime.utcnow() - timedelta(days=180)
    cand_rows = db.execute(
        select(ReviewMention.name, func.count(ReviewMention.id), func.max(Review.created_at_source))
        .join(Review, ReviewMention.review_id == Review.id)
        .where(ReviewMention.employee_id.is_(None), Review.created_at_source >= since)
        .group_by(ReviewMention.name).order_by(func.count(ReviewMention.id).desc()).limit(40)).all()
    # most common site per candidate
    candidates = []
    for name, n, last in cand_rows:
        site = db.execute(select(Location.id, Location.name, func.count(Review.id))
                          .join(ReviewSourceLink, ReviewSourceLink.location_id == Location.id)
                          .join(Review, Review.source_link_id == ReviewSourceLink.id)
                          .join(ReviewMention, ReviewMention.review_id == Review.id)
                          .where(ReviewMention.name == name).group_by(Location.id, Location.name)
                          .order_by(func.count(Review.id).desc()).limit(1)).first()
        candidates.append({"name": name, "count": n, "last": last, "location_id": site[0] if site else None, "site": site[1] if site else ""})
    editing = db.get(Employee, edit) if edit else None
    return _render(request, "admin_employees.html", user, db, employees=employees, locs=locs, candidates=candidates, editing=editing)


@app.post("/admin/employees")
def save_employee(user: User = Depends(auth.admin_user), db: Session = Depends(get_db), id: int = Form(0), name: str = Form(...),
                  aliases: str = Form(""), location_id: int = Form(0), role: str = Form(""), active: int = Form(1)):
    e = db.get(Employee, id) if id else Employee()
    if not id:
        db.add(e)
    e.name, e.aliases, e.role = name.strip()[:80], aliases.strip() or None, role.strip() or None
    e.location_id, e.active = location_id or None, bool(active)
    db.flush()
    # link existing mentions that match any of the names
    names = {n.lower() for n in e.all_names()}
    for m in db.execute(select(ReviewMention).where(ReviewMention.employee_id.is_(None))).scalars().all():
        if m.name.lower() in names:
            m.employee_id = e.id
            m.name = e.name
    db.commit()
    return RedirectResponse(url="/admin/employees", status_code=303)


@app.post("/admin/employees/{eid}/delete")
def delete_employee(eid: int, user: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    e = db.get(Employee, eid)
    if e:
        for m in db.execute(select(ReviewMention).where(ReviewMention.employee_id == e.id)).scalars().all():
            m.employee_id = None
        db.delete(e)
        db.commit()
    return RedirectResponse(url="/admin/employees", status_code=303)


@app.post("/admin/employees/reindex")
def reindex_mentions(background: BackgroundTasks, user: User = Depends(auth.admin_user)):
    def _run():
        with session_scope() as s:
            from .text_intel import build_roster
            roster = build_roster(s)
            emps = {e.name: e.id for e in s.execute(select(Employee)).scalars().all()}
            for r in s.execute(select(Review).where(Review.is_deleted.is_(False)).options(selectinload(Review.mentions))).scalars().all():
                apply_intel(s, r, roster, emps)
    background.add_task(_run)
    return RedirectResponse(url="/admin/employees", status_code=303)


@app.get("/admin/ai", response_class=HTMLResponse)
def admin_ai(request: Request, user: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    rules = db.execute(select(AiRule).order_by(AiRule.sort_order, AiRule.id)).scalars().all()
    stats = db.execute(select(func.count(ReplyRow.id)).where(ReplyRow.ai_generated.is_(True), ReplyRow.status == "posted")).scalar() or 0
    return _render(request, "admin_ai.html", user, db, rules=rules, ai_enabled=settings.ai_enabled, model=settings.ai_model,
                   phones=settings.brand_phones, ai_posted=stats)


@app.post("/admin/ai/classify")
def ai_classify_window(background: BackgroundTasks, user: User = Depends(auth.admin_user), range: str = Form("last30"),
                       start: str = Form(""), end: str = Form(""), force: int = Form(0), back: str = Form("/reports")):
    if not settings.ai_enabled:
        raise HTTPException(400, "AI is not configured")
    dr = _dr(range, start, end)

    def _run():
        from .ai import AiUnavailable, classify_negative
        with session_scope() as s:
            q = select(Review).where(Review.is_deleted.is_(False), Review.rating <= settings.negative_rating_max,
                                     Review.created_at_source >= dr.start, Review.created_at_source < dr.end)
            for r in s.execute(q).scalars().all():
                if not (r.text or "").strip():
                    r.category = "No Content"; continue
                if r.category and not force and r.category != "Unknown":
                    continue
                try:
                    r.category = classify_negative(r, THEMES)
                except AiUnavailable:
                    break
    background.add_task(_run)
    return RedirectResponse(url=back, status_code=303)


@app.post("/admin/ai")
def save_rule(user: User = Depends(auth.admin_user), db: Session = Depends(get_db), id: int = Form(0), text: str = Form(...),
              sort_order: int = Form(100), active: int = Form(1)):
    r = db.get(AiRule, id) if id else AiRule()
    if not id:
        db.add(r)
    r.text, r.sort_order, r.active = text.strip(), sort_order, bool(active)
    db.commit()
    return RedirectResponse(url="/admin/ai", status_code=303)


@app.post("/admin/ai/{rid}/delete")
def delete_rule(rid: int, user: User = Depends(auth.admin_user), db: Session = Depends(get_db)):
    r = db.get(AiRule, rid)
    if r:
        db.delete(r)
        db.commit()
    return RedirectResponse(url="/admin/ai", status_code=303)
