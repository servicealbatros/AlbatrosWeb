"""Advisor package routes, inside the existing authenticated dashboard boundary."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
import dsm_automatic_codes as codes

router = APIRouter()


def require_advisor(request):
    if not getattr(request.state, 'dashboard_user', None):
        raise HTTPException(401, 'Autentificare consilier necesară.')


@router.get('/baza-dsm/pachete')
def page(request: Request):
    require_advisor(request)
    return RedirectResponse('/baza-dsm', status_code=303, headers={'Cache-Control': 'no-store'})


@router.get('/api/dashboard/parts-packages/vehicles')
def vehicles(request: Request, query: str='', limit: int=20):
    require_advisor(request)
    try:
        return JSONResponse(codes.vehicles(query, limit), headers={'Cache-Control': 'no-store'})
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc))


@router.get('/api/dashboard/parts-packages/lookup')
def lookup(request: Request, registration: str='', vin: str='', work: str='revision'):
    require_advisor(request)
    try:
        return JSONResponse(codes.lookup(registration, vin, work), headers={'Cache-Control': 'no-store'})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc))
