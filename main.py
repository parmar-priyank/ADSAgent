# App entry point — wires everything together
from core import (
    app,
    limiter,
    _on_rate_limit_exceeded,
    _auth_redirect_handler,
    _AuthRedirect,
    _CSP,
    SecurityHeadersMiddleware,
    _FAVICON,
)
from slowapi.errors import RateLimitExceeded
from fastapi import Response
from routers import auth, admin, qc, pdf, templates as tpl_router

app.add_exception_handler(RateLimitExceeded, _on_rate_limit_exceeded)
app.add_exception_handler(_AuthRedirect, _auth_redirect_handler)
app.add_middleware(SecurityHeadersMiddleware)

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(qc.router)
app.include_router(pdf.router)
app.include_router(tpl_router.router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=_FAVICON, media_type="image/svg+xml")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
