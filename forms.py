import math
import time
from datetime import datetime
from functools import wraps

import pandas as pd
import pytz
import streamlit as st
from streamlit_gsheets import GSheetsConnection

# ══════════════════════════════════════════════════════════════════
# CONFIGURAÇÕES GERAIS
# ══════════════════════════════════════════════════════════════════

st.set_page_config(page_title="Lista de Presença - IX ENCES", page_icon="✅")

# Fuso horário do evento
FUSO = pytz.timezone("America/Sao_Paulo")


EVENTO_LAT = -15.78798   # ← latitude do local do evento
EVENTO_LON = -47.91416   # ← longitude do local do evento
RAIO_MAXIMO_METROS = 2000  # raio em metros (300m é bem preciso para um prédio)

TURNOS = {
    "presenca_dia1_manha": {
        "label": "📅 Dia 1 — Manhã",
        "inicio": datetime(2026, 9, 8,  8,  0, tzinfo=FUSO),
        "fim":    datetime(2026, 9, 8, 18, 30, tzinfo=FUSO),
    },
    "presenca_dia1_tarde": {
        "label": "📅 Dia 1 — Tarde",
        "inicio": datetime(2026, 11, 4, 13,  30, tzinfo=FUSO),
        "fim":    datetime(2026, 11, 4, 18,  30, tzinfo=FUSO),
    },
    "presenca_dia2_manha": {
        "label": "📅 Dia 2 — Manhã",
        "inicio": datetime(2026, 11, 5,  8,  0, tzinfo=FUSO),
        "fim":    datetime(2026, 11, 5, 12, 30, tzinfo=FUSO),
    },
    "presenca_dia2_tarde": {
        "label": "📅 Dia 2 — Tarde",
        "inicio": datetime(2026, 11, 5, 13,  30, tzinfo=FUSO),
        "fim":    datetime(2026, 11, 5, 18,  30, tzinfo=FUSO),
    },
}

COLUNAS_PRESENCA = list(TURNOS.keys())


# ══════════════════════════════════════════════════════════════════
# GEOLOCALIZAÇÃO
# ══════════════════════════════════════════════════════════════════

def calcular_distancia_metros(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """
    Fórmula de Haversine.
    Retorna a distância em metros entre dois pontos geográficos.
    """
    R = 6_371_000  # raio da Terra em metros
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return round(2 * R * math.asin(math.sqrt(a)))


def verificar_dentro_do_evento(lat: float, lon: float) -> tuple[bool, int]:
    """
    Verifica se a coordenada está dentro do raio permitido.
    Retorna (True/False, distância_em_metros).
    """
    distancia = calcular_distancia_metros(lat, lon, EVENTO_LAT, EVENTO_LON)
    return distancia <= RAIO_MAXIMO_METROS, distancia


def obter_localizacao() -> dict | None:
    """
    Usa streamlit-js-eval para capturar a geolocalização do dispositivo via browser.
    Retorna o objeto de geolocalização ou None.
    """
    try:
        from streamlit_js_eval import get_geolocation
        return get_geolocation()
    except ImportError:
        st.error("❌ Pacote não encontrado. Execute: pip install streamlit-js-eval")
        return None


# ══════════════════════════════════════════════════════════════════
# TURNO ATIVO
# ══════════════════════════════════════════════════════════════════

def get_turno_ativo() -> tuple[str | None, str | None]:
    """
    Verifica o horário atual e retorna (chave_do_turno, label_do_turno).
    Retorna (None, None) se nenhum turno estiver aberto.
    """
    agora = datetime.now(FUSO)
    for chave, turno in TURNOS.items():
        if turno["inicio"] <= agora <= turno["fim"]:
            return chave, turno["label"]
    return None, None


# ══════════════════════════════════════════════════════════════════
# GOOGLE SHEETS — LEITURA E ESCRITA
# ══════════════════════════════════════════════════════════════════

def retry_sheets(max_retries: int = 3, initial_delay: int = 1):
    """Decorator com retry + exponential backoff para operações no Google Sheets."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exc = None
            for attempt in range(max_retries):
                try:
                    result = func(*args, **kwargs)
                    # Se retornou DataFrame vazio, tenta novamente
                    if isinstance(result, pd.DataFrame) and result.empty:
                        raise ValueError("DataFrame vazio")
                    if isinstance(result, tuple) and any(
                        isinstance(r, pd.DataFrame) and r.empty for r in result
                    ):
                        raise ValueError("DataFrame vazio em tuple")
                    return result
                except Exception as exc:
                    last_exc = exc
                    if attempt < max_retries - 1:
                        st.warning(f"⚠️ Tentativa {attempt + 1} falhou. Aguardando {delay}s...")
                        time.sleep(delay)
                        st.cache_data.clear()
                        delay *= 2
            st.error(f"❌ Todas as {max_retries} tentativas falharam: {last_exc}")
            raise last_exc
        return wrapper
    return decorator


@st.cache_data(ttl=5)
@retry_sheets(max_retries=3, initial_delay=1)
def ler_dados_sheets() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Lê as duas abas do Google Sheets:
      - 'presencas'   : registros de presença já salvos
      - 'lista_evento': lista oficial de participantes (co_ies, no_ies, no_pessoa_fisica)
    """
    conn = st.connection("gsheets", type=GSheetsConnection)
    # 6 colunas: nome_participante, nome_ies + 4 colunas de presença
    presencas    = conn.read(worksheet="presencas",    usecols=list(range(6)))
    lista_evento = conn.read(worksheet="lista_evento", usecols=[0, 1, 2])
    return presencas, lista_evento


@retry_sheets(max_retries=3, initial_delay=1)
def registrar_presenca_sheets(ies: str, participante: str, turno_col: str) -> bool:
    """
    Salva a presença no Google Sheets.
    - Se já existe linha para o participante → atualiza apenas a coluna do turno.
    - Se não existe → cria nova linha.
    Grava o timestamp no formato dd/mm/aaaa hh:mm:ss.
    """
    conn = st.connection("gsheets", type=GSheetsConnection)
    st.cache_data.clear()
    presencas, _ = ler_dados_sheets()

    agora = datetime.now(FUSO).strftime("%d/%m/%Y %H:%M:%S")

    # Garante que todas as colunas necessárias existam no DataFrame
    todas_colunas = ["nome_participante", "nome_ies"] + COLUNAS_PRESENCA
    for col in todas_colunas:
        if col not in presencas.columns:
            presencas[col] = ""

    mask = presencas["nome_participante"] == participante

    if mask.any():
        # Atualiza somente a coluna do turno ativo
        presencas.loc[mask, turno_col] = agora
    else:
        # Cria nova linha com todas as colunas
        nova_linha = {col: "" for col in todas_colunas}
        nova_linha["nome_participante"] = participante
        nova_linha["nome_ies"]          = ies
        nova_linha[turno_col]           = agora
        presencas = pd.concat([presencas, pd.DataFrame([nova_linha])], ignore_index=True)

    conn.update(worksheet="presencas", data=presencas)
    st.cache_data.clear()
    return True


# ══════════════════════════════════════════════════════════════════
# HELPERS DE PARTICIPANTES
# ══════════════════════════════════════════════════════════════════

def get_iniciais(nome: str) -> str:
    """Converte nome completo para iniciais (ex: 'João da Silva' → 'J D S')."""
    return " ".join(p[0].upper() for p in nome.split())


def get_ies_list() -> list[str]:
    """Retorna lista de IES formatada como 'codigo - nome'."""
    _, lista_evento = ler_dados_sheets()
    lista_evento["ies_completo"] = (
        lista_evento["co_ies"].astype(int).astype(str) + " — " + lista_evento["no_ies"]
    )
    return sorted(lista_evento["ies_completo"].unique().tolist())


def get_participantes_ies(ies_selecionada: str) -> list[str]:
    """
    Filtra participantes pela IES e retorna lista de iniciais.
    Mantém mapeamento iniciais → nome_completo no session_state.
    """
    _, lista_evento = ler_dados_sheets()
    co_ies = int(ies_selecionada.split(" — ")[0])
    participantes = (
        lista_evento[lista_evento["co_ies"].astype(int) == co_ies]["no_pessoa_fisica"]
        .dropna()
        .tolist()
    )

    if "mapeamento_nomes" not in st.session_state:
        st.session_state.mapeamento_nomes = {}

    participantes_iniciais = []
    for nome in participantes:
        iniciais = get_iniciais(nome)
        base, contador = iniciais, 1
        # Evita colisão de iniciais idênticas
        while (
            iniciais in st.session_state.mapeamento_nomes
            and st.session_state.mapeamento_nomes[iniciais] != nome
        ):
            iniciais = f"{base} ({contador})"
            contador += 1
        st.session_state.mapeamento_nomes[iniciais] = nome
        participantes_iniciais.append(iniciais)

    return sorted(participantes_iniciais)


def verificar_presenca_existente(participante: str, turno_col: str) -> bool:
    """Retorna True se o participante já registrou presença no turno informado."""
    presencas, _ = ler_dados_sheets()
    registro = presencas[presencas["nome_participante"] == participante]
    if registro.empty:
        return False
    valor = registro.iloc[0].get(turno_col, None)
    return pd.notna(valor) and str(valor).strip() != ""


def get_presencas_participante(participante: str) -> dict:
    """Retorna dicionário com os turnos e seus respectivos registros do participante."""
    presencas, _ = ler_dados_sheets()
    registro = presencas[presencas["nome_participante"] == participante]
    if registro.empty:
        return {}
    row = registro.iloc[0]
    return {
        TURNOS[col]["label"]: row.get(col, "") or "—"
        for col in COLUNAS_PRESENCA
    }


# ══════════════════════════════════════════════════════════════════
# INTERFACE PRINCIPAL
# ══════════════════════════════════════════════════════════════════

def main():
    # Tente desabilitar o comentário abaixo se tiver logo do evento
    #st.image(Image.open("logo.png").resize((400, 200)))
    st.title("✅ Lista de Presença — XI ENCES")
    st.divider()

    # ── PASSO 1: Verificar turno ativo ────────────────────────────
    turno_col, turno_label = get_turno_ativo()

    if not turno_col:
        st.warning("⏰ **Nenhum turno de presença está aberto no momento.**")
        st.markdown("#### Horários de registro:")
        for chave, t in TURNOS.items():
            inicio_str = t["inicio"].strftime("%d/%m às %H:%M")
            fim_str    = t["fim"].strftime("%H:%M")
            st.write(f"• **{t['label']}**: {inicio_str} — {fim_str}h")
        return

    st.success(f"🟢 Turno aberto: **{turno_label}**")
    st.divider()

    # ── PASSO 2: Verificar geolocalização ─────────────────────────
    st.markdown("### 📍 Verificação de localização")
    st.caption(
        f"O registro de presença só é permitido a até **{RAIO_MAXIMO_METROS} metros** "
        "do local do evento. Permita o acesso à sua localização quando solicitado."
    )

    loc = obter_localizacao()

    if loc is None:
        st.info("⏳ Aguardando permissão de localização do dispositivo...")
        return

    lat = loc.get("coords", {}).get("latitude")
    lon = loc.get("coords", {}).get("longitude")

    if lat is None or lon is None:
        st.error(
            "❌ Não foi possível obter sua localização. "
            "Verifique se o GPS está ativo e permita o acesso ao site."
        )
        return

    dentro, distancia = verificar_dentro_do_evento(lat, lon)

    if not dentro:
        st.error(
            f"🚫 Você está a **{distancia}m** do local do evento. "
            f"O registro só é permitido a até {RAIO_MAXIMO_METROS}m."
        )
        return

    st.success(f"✅ Localização confirmada! Você está a **{distancia}m** do evento.")
    st.divider()

    # ── PASSO 3: Selecionar IES ───────────────────────────────────
    st.markdown("### 🏫 Selecione sua Instituição")
    ies_options = ["Selecione uma IES..."] + list(get_ies_list())
    ies = st.selectbox("IES", options=ies_options, label_visibility="collapsed")

    if ies == "Selecione uma IES...":
        return

    # ── PASSO 4: Selecionar Participante ──────────────────────────
    st.markdown("### 👤 Selecione o Participante")
    participantes_iniciais = get_participantes_ies(ies)

    if not participantes_iniciais:
        st.error("⚠️ Nenhum participante cadastrado para esta IES.")
        return

    participante_iniciais = st.selectbox(
        "Participante",
        options=["Selecione um participante..."] + participantes_iniciais,
        label_visibility="collapsed",
    )

    if participante_iniciais == "Selecione um participante...":
        return

    nome_completo = st.session_state.mapeamento_nomes[participante_iniciais]

    # ── PASSO 5: Verificar presença duplicada ─────────────────────
    if verificar_presenca_existente(nome_completo, turno_col):
        st.warning(
            f"⚠️ **{participante_iniciais}** já registrou presença para **{turno_label}**.\n\n"
            "Cada turno permite apenas **um registro por participante**."
        )
        # Mostra histórico do participante
        historico = get_presencas_participante(nome_completo)
        if historico:
            st.markdown("**Histórico de presenças:**")
            for turno, horario in historico.items():
                icone = "✅" if horario != "—" else "⬜"
                st.write(f"{icone} {turno}: {horario}")
        return

    # ── PASSO 6: Confirmar e registrar ───────────────────────────
    st.divider()
    st.markdown("### 📋 Confirmação de Presença")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**Participante:** {participante_iniciais}")
        st.markdown(f"**IES:** {ies}")
    with col2:
        st.markdown(f"**Turno:** {turno_label}")
        st.markdown(f"**Localização:** ✅ Confirmada ({distancia}m)")

    st.markdown("")  # espaçamento

    if st.button("✅ Confirmar Presença", type="primary", use_container_width=True):
        with st.spinner("Registrando presença..."):
            sucesso = registrar_presenca_sheets(ies, nome_completo, turno_col)

        if sucesso:
            st.success("🎉 **Presença registrada com sucesso!**")
            st.balloons()
            st.cache_data.clear()
            time.sleep(2)
            st.rerun()
        else:
            st.error("❌ Não foi possível registrar a presença. Tente novamente.")
            st.cache_data.clear()


if __name__ == "__main__":
    main()
