"""
Outlook token bridge — permite que a janela de relay (aberta no Edge)
deposite o token via POST, e que o app React faça polling via GET.

Fluxo:
  1. React chama POST /api/outlook/open  → backend abre Edge em outlook.office.com/mail
  2. Usuário roda o bookmarklet na página do Outlook
  3. Bookmarklet abre /outlook-relay.html (no Edge) com o token no hash
  4. relay.html faz POST /api/outlook/token com o token
  5. React faz polling em GET /api/outlook/token até receber o token
  6. Token salvo no localStorage do React; bridge limpa o estado
"""
import subprocess
import threading
import time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/outlook", tags=["outlook"])

# Estado em memória — token pendente entre o relay e o React
_pending: dict = {}   # { token, expires_on, received_at }
_lock = threading.Lock()


class TokenPayload(BaseModel):
    token: str
    expires_on: int   # unix timestamp (seconds)


@router.post("/open")
def open_outlook():
    """Abre o Edge em outlook.office.com/mail para o usuário iniciar o fluxo."""
    try:
        subprocess.Popen(
            ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
             "--new-window",
             "https://outlook.office.com/mail"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"ok": True}
    except FileNotFoundError:
        # Fallback: abre com o handler padrão do sistema
        subprocess.Popen(["open", "-a", "Microsoft Edge", "https://outlook.office.com/mail"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "fallback": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/token")
def receive_token(payload: TokenPayload):
    """Relay deposita o token aqui depois de extraí-lo do Outlook."""
    with _lock:
        _pending["token"]       = payload.token
        _pending["expires_on"]  = payload.expires_on
        _pending["received_at"] = time.time()
    return {"ok": True}


@router.get("/token")
def poll_token():
    """
    React faz polling aqui. Retorna o token se disponível (e ele foi
    recebido nos últimos 5 minutos). Limpa após servir.
    """
    with _lock:
        if not _pending.get("token"):
            return {"ready": False}
        age = time.time() - _pending.get("received_at", 0)
        if age > 300:
            _pending.clear()
            return {"ready": False}
        data = {
            "ready":      True,
            "token":      _pending["token"],
            "expires_on": _pending["expires_on"],
        }
        _pending.clear()
        return data


@router.delete("/token")
def clear_token():
    """Limpa o estado (cancelamento pelo usuário)."""
    with _lock:
        _pending.clear()
    return {"ok": True}
