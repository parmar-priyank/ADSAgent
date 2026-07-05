# App entry point — wires everything together
from config import (
    app,
    limiter,
    _on_rate_limit_exceeded,
    _auth_redirect_handler,
    _AuthRedirect,
    _CSP,
    SecurityHeadersMiddleware,
    InactivityTimeoutMiddleware,
    UserLoginIPRestrictionMiddleware,
    _FAVICON,
)
from slowapi.errors import RateLimitExceeded
from fastapi import Response
from routers import auth, admin, uploads, qc_checks, checklist_mgmt

app.add_exception_handler(RateLimitExceeded, _on_rate_limit_exceeded)
app.add_exception_handler(_AuthRedirect, _auth_redirect_handler)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(InactivityTimeoutMiddleware)
app.add_middleware(UserLoginIPRestrictionMiddleware)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(uploads.router)
app.include_router(qc_checks.router)
app.include_router(checklist_mgmt.router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=_FAVICON, media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        workers=4,          # 4 independent processes — up to 4 users run heavy jobs in parallel
        loop="uvloop",      # faster async event loop (included in uvicorn[standard])
    )
