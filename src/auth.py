"""Autenticação simples por e-mail e senha para o tracker de reprocesso.

Ativado quando APP_EMAIL e APP_PASS estão definidos no ambiente.
Em desenvolvimento local (sem essas variáveis), a autenticação é desabilitada
e qualquer acesso é permitido automaticamente.

Uso em main():
    auth.build_login_page()
    # Em cada página protegida:
    if not auth.is_authenticated():
        ui.navigate.to("/login")
        return
"""

from __future__ import annotations

import os

from nicegui import app as _nicegui_app, ui

_EMAIL = os.environ.get("APP_EMAIL", "")
_PASS = os.environ.get("APP_PASS", "")

AUTH_ENABLED: bool = bool(_EMAIL and _PASS)


def is_authenticated() -> bool:
    """True se o usuário está autenticado ou se auth está desabilitada."""
    if not AUTH_ENABLED:
        return True
    return bool(_nicegui_app.storage.user.get("authenticated"))


def logout() -> None:
    """Encerra a sessão e redireciona para /login."""
    _nicegui_app.storage.user["authenticated"] = False
    ui.navigate.to("/login")


def build_login_page() -> None:
    """Registra a rota /login. Deve ser chamado uma vez em main()."""

    @ui.page("/login")
    def _login_page():
        if is_authenticated():
            ui.navigate.to("/")
            return

        ui.add_head_html(
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
        )

        with ui.card().classes("absolute-center shadow-4").style("width:360px"):
            ui.label("LACEN-RS").classes(
                "text-caption text-grey-6 text-center w-full q-mt-xs"
            )
            ui.label("Reprocesso Dengue").classes(
                "text-h6 text-bold text-center w-full"
            )
            ui.separator().classes("q-mb-sm")

            email = (
                ui.input("E-mail", placeholder="usuario@lacen.rs.gov.br")
                .props("type=email outlined dense")
                .classes("w-full q-mt-sm")
            )
            senha = (
                ui.input("Senha", password=True, password_toggle_button=True)
                .props("outlined dense")
                .classes("w-full q-mt-xs")
            )

            msg_erro = ui.label("").classes("text-red-7 text-caption")

            def _tentar_login() -> None:
                if email.value.strip() == _EMAIL and senha.value == _PASS:
                    _nicegui_app.storage.user["authenticated"] = True
                    ui.navigate.to("/")
                else:
                    msg_erro.text = "E-mail ou senha incorretos."
                    senha.value = ""

            ui.button("Entrar", on_click=_tentar_login, icon="login").props(
                "color=primary unelevated"
            ).classes("w-full q-mt-sm q-mb-xs")

            email.on("keydown.enter", lambda _: _tentar_login())
            senha.on("keydown.enter", lambda _: _tentar_login())
