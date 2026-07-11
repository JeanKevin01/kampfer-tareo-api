# ============================================================
# routers/media.py — sirve las fotos de campo con URL firmada.
#
# GET /media/{ruta}?exp=<epoch>&sig=<hmac>
# La firma ES la credencial (los <img> no llevan Authorization); require_key
# deja pasar /media/* y aquí se valida firma + expiración + confinamiento
# de la ruta dentro de MEDIA_DIR. Se monta en main.py SIN require_role.
# ============================================================
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from core.media import firma_valida, resolver_ruta

router = APIRouter(tags=["media"])


@router.get("/media/{ruta:path}")
async def servir_media(ruta: str, exp: str = "", sig: str = ""):
    if not firma_valida(ruta, exp, sig):
        raise HTTPException(403, "URL inválida o vencida; recarga la página")
    destino = resolver_ruta(ruta)
    if destino is None:
        raise HTTPException(403, "Ruta no permitida")
    if not destino.is_file():
        raise HTTPException(404, "La imagen no existe (posiblemente purgada)")
    return FileResponse(destino, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=900"})
