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
RAIO_MAXIMO_METROS = 9999999999999999999999  # raio em metros (300m é bem preciso para um prédio)

TURNOS = {
    "presenca_dia1_manha": {
        "label":  "📅 Dia 1 — Manhã",
        "inicio": datetime(2026, 9, 9,  7,  40, tzinfo=FUSO),
        "fim":    datetime(2026, 9, 9, 10, 00, tzinfo=FUSO),
    },
    "presenca_dia1_tarde": {
        "label":  "📅 Dia 1 — Tarde",
        "inicio": datetime(2026, 9, 9, 10, 40, tzinfo=FUSO),
        "fim":    datetime(2026, 9, 9, 12, 49, tzinfo=FUSO),
    },
    "presenca_dia2_manha": {
        "label":  "📅 Dia 2 — Manhã",
        "inicio": datetime(2026, 9, 9,  10,  10, tzinfo=FUSO),
        "fim":    datetime(2026, 9, 9, 12, 00, tzinfo=FUSO),
    },
    "presenca_dia2_tarde": {
        "label":  "📅 Dia 2 — Tarde",
        "inicio": datetime(2026, 9, 9, 8, 55, tzinfo=FUSO),
        "fim":    datetime(2026, 9, 9, 8, 59, tzinfo=FUSO),
    },
}


COLUNAS_PRESENCA = list(TURNOS.keys())
TODAS_COLUNAS    = ["nome_participante", "nome_ies"] + COLUNAS_PRESENCA



# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def timestamp_agora() -> str:
    """
    Formato que o Google Sheets NÃO interpreta como data.
    Barras e dois-pontos fazem o Sheets converter para serial float.
    Usando traço e 'h' ele trata como texto puro.
    """
    return datetime.now(FUSO).strftime("%d-%m-%Y %Hh%M")


def celula_preenchida(valor) -> bool:
    """True se a célula tem valor real (não vazio, não NaN)."""
    if valor is None:
        return False
    return pd.notna(valor) and str(valor).strip() not in ("", "nan", "NaN", "None")


def garantir_colunas(df: pd.DataFrame) -> pd.DataFrame:
    """
    Garante que o DataFrame tenha todas as colunas na ordem correta,
    e que as colunas de presença sejam dtype object (string).

    FIX BUG 1: Quando o Sheets retorna colunas vazias, pandas as infere
    como float64. Tentar salvar uma string nelas causa:
      "Invalid value '09-09-2026 09h15' for dtype 'float64'"
    A solução é forçar .astype(str) e limpar os "nan" strings.
    """
    df = df.copy()

    # Cria colunas faltantes
    for col in TODAS_COLUNAS:
        if col not in df.columns:
            df[col] = ""

    # Reordena
    df = df[TODAS_COLUNAS]

    # FIX: força dtype string em TODAS as colunas de presença
    for col in COLUNAS_PRESENCA:
        df[col] = (
            df[col]
            .fillna("")           # NaN → ""
            .astype(str)          # float64 "nan" → string "nan"
            .str.strip()
            .replace({"nan": "", "NaN": "", "None": "", "NaT": ""})
        )

    # Garante string em nome_participante e nome_ies também
    df["nome_participante"] = df["nome_participante"].fillna("").astype(str).str.strip()
    df["nome_ies"]          = df["nome_ies"].fillna("").astype(str).str.strip()

    return df


# ══════════════════════════════════════════════════════════════════
# LEITURA — cache de 30s para respeitar quota da API
# ══════════════════════════════════════════════════════════════════
#
# FIX BUG 2 — Rate limit:
#
# PROBLEMA ANTERIOR:
#   ttl=1s → cache expira quase instantaneamente
#   cache_data.clear() era chamado ANTES de ler (invalidava cache de todos)
#   retry limpava cache a cada tentativa → efeito cascata com múltiplos usuários
#
# SOLUÇÃO:
#   ttl=30s → cada usuário lê no máximo 2x/min individualmente
#   cache_data.clear() APENAS após escrita bem-sucedida
#   retry de leitura NÃO limpa cache (evita avalanche)
#   backoff maior (initial_delay=2s)
#
# IMPORTANTE: a cota do Google Sheets é 60 leituras/min por service account,
# compartilhada entre todos os usuários simultâneos. Com ttl=1s e 10 pessoas,
# são ~600 req/min → estoura. Com ttl=30s e 10 pessoas → ~20 req/min → ok.

def retry_leitura(max_retries=3, initial_delay=2):
    """
    Retry para leituras — NÃO limpa cache entre tentativas.
    Limpar cache em retry de leitura causa avalanche quando há múltiplos usuários.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay          = initial_delay
            last_exception = None
            for attempt in range(max_retries):
                try:
                    result = func(*args, **kwargs)
                    if isinstance(result, tuple):
                        if any(isinstance(r, pd.DataFrame) and r.empty for r in result):
                            raise ValueError("DataFrame vazio")
                    elif isinstance(result, pd.DataFrame) and result.empty:
                        raise ValueError("DataFrame vazio")
                    return result
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        st.warning(f"⚠️ Tentativa {attempt + 1} falhou. Aguardando {delay}s...")
                        time.sleep(delay)
                        # NÃO limpa cache aqui — evita invalidar cache de outros usuários
                        delay *= 2
            st.error(f"❌ Falha na leitura após {max_retries} tentativas: {str(last_exception)}")
            raise last_exception
        return wrapper
    return decorator


def retry_escrita(max_retries=3, initial_delay=2):
    """
    Retry para escritas — pode limpar cache após cada tentativa,
    pois o objetivo é garantir que o dado seja salvo.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay          = initial_delay
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        st.warning(f"⚠️ Tentativa de escrita {attempt + 1} falhou. Aguardando {delay}s...")
                        time.sleep(delay)
                        delay *= 2
            st.error(f"❌ Falha na escrita após {max_retries} tentativas: {str(last_exception)}")
            raise last_exception
        return wrapper
    return decorator


@st.cache_data(ttl=30)   # 30s: suficiente para não mostrar dado stale, mas respeitando quota
@retry_leitura(max_retries=3, initial_delay=2)
def ler_dados_sheets():
    """
    Lê presencas e lista_evento.
    Cache de 30s — essencial para não estourar quota com múltiplos usuários.
    """
    conn         = st.connection('gsheets', type=GSheetsConnection)
    presencas    = conn.read(worksheet="presencas",    usecols=list(range(6)))
    lista_evento = conn.read(worksheet="lista_evento", usecols=[0, 1, 2])
    return presencas, lista_evento


# ══════════════════════════════════════════════════════════════════
# BACKUP
# ══════════════════════════════════════════════════════════════════

def fazer_backup(conn, df_atual: pd.DataFrame) -> bool:
    """
    Salva cópia em 'backup' com timestamp.
    Nunca salva DF vazio. Falha não interrompe o fluxo principal.
    """
    try:
        if df_atual.empty:
            st.warning("⚠️ Backup ignorado: DataFrame vazio.")
            return False
        df_backup              = df_atual.copy()
        df_backup["backup_em"] = timestamp_agora()
        conn.update(worksheet="backup", data=df_backup)
        return True
    except Exception as exc:
        st.warning(f"⚠️ Backup não realizado (dados principais NÃO afetados): {exc}")
        return False


# ══════════════════════════════════════════════════════════════════
# REGISTRO DE PRESENÇA
# ══════════════════════════════════════════════════════════════════

@retry_escrita(max_retries=3, initial_delay=2)
def adicionar_presenca(ies: str, participante: str, turno_col: str) -> bool:
    """
    Registra presença com proteção real contra conflito simultâneo.

    Problema do "mesmo segundo":
      Dois usuários lêem a planilha no mesmo instante (ex: 100 linhas).
      Ambos gravam 101 linhas — o segundo sobrescreve o primeiro.
      Leitura fresca antes de gravar NÃO resolve, pois ambos lêem
      o mesmo estado antes de qualquer um ter gravado.

    Solução — verificação pós-escrita:
      1. Lê planilha (base para merge)
      2. Aplica alteração e grava
      3. Aguarda 1s e lê novamente para CONFIRMAR que o registro está lá
      4. Se não estiver → alguém sobrescreveu no mesmo instante
         → lança RuntimeError → retry_escrita aguarda e tenta de novo
      5. Na nova tentativa, a leitura já inclui o registro do outro usuário
         → merge correto → nenhum dado perdido

    Consumo de API por registro (caso normal):
      • 2 leituras (base + verificação pós-escrita)
      • 1 escrita
    Em caso de conflito real (raro):
      • +2 leituras e +1 escrita na retentativa
    """
    conn  = st.connection('gsheets', type=GSheetsConnection)
    agora = timestamp_agora()

    # ── PASSO 1: Leitura base (para merge e checagem de duplicidade) ──
    presencas = garantir_colunas(
        conn.read(worksheet="presencas", usecols=list(range(6)))
    )

    mask = presencas["nome_participante"] == participante

    # Checagem de duplicidade — aborta se já registrou neste turno
    if mask.any():
        valor_atual = presencas.loc[mask, turno_col].iloc[0]
        if celula_preenchida(valor_atual):
            return False  # Já registrou — UI já exibe aviso, não precisa de erro

    # ── PASSO 2: Monta data_final sobre a leitura base ────────────
    if mask.any():
        # UPDATE: participante já tem linha — atualiza só o turno atual
        presencas.loc[mask, turno_col] = agora
        data_final = presencas
    else:
        # INSERT: participante novo — adiciona linha ao final
        nova_linha = {col: "" for col in TODAS_COLUNAS}
        nova_linha["nome_participante"] = participante
        nova_linha["nome_ies"]          = ies
        nova_linha[turno_col]           = agora
        data_final = pd.concat(
            [presencas, pd.DataFrame([nova_linha])],
            ignore_index=True
        )

    # Validação de integridade antes de gravar
    if len(data_final) < len(presencas):
        raise ValueError(
            f"Abortado: DF ficou com {len(data_final)} linhas "
            f"(era {len(presencas)}). Dados originais preservados."
        )

    # ── PASSO 3: Grava ───────────────────────────────────────────
    conn.update(worksheet="presencas", data=data_final)

    # ── PASSO 4: Verificação pós-escrita (anti "mesmo segundo") ──
    #
    # Aguarda 1s para o Google Sheets propagar a escrita,
    # depois lê novamente e confirma que nosso registro está lá.
    #
    # Cenário de conflito:
    #   A grava 101 linhas (A)
    #   B grava 101 linhas (B) ← sobrescreve A no mesmo instante
    #   A verifica → A não está → RuntimeError → retry_escrita aguarda 2s
    #   A tenta novamente → lê 101 linhas (B já está lá) → merge → grava 102 linhas
    #   B verifica → B está lá → sucesso ✅
    #   A verifica → A está lá → sucesso ✅
    #
    time.sleep(1)

    verificacao = garantir_colunas(
        conn.read(worksheet="presencas", usecols=list(range(6)))
    )

    mask_verif = verificacao["nome_participante"] == participante
    registro_confirmado = (
        mask_verif.any()
        and celula_preenchida(
            verificacao.loc[mask_verif, turno_col].iloc[0]
        )
    )

    if not registro_confirmado:
        # Conflito simultâneo detectado — retry_escrita vai tentar novamente.
        # Na próxima tentativa, a leitura base já terá o registro do outro usuário,
        # garantindo merge correto sem perda de dados.
        raise RuntimeError(
            "Conflito simultâneo: registro não confirmado na planilha. "
            "Fazendo nova tentativa com dados atualizados..."
        )

    # Cache limpo SOMENTE após confirmação real da escrita
    st.cache_data.clear()
    return True


# ══════════════════════════════════════════════════════════════════
# GEOLOCALIZAÇÃO
# ══════════════════════════════════════════════════════════════════

def calcular_distancia_metros(lat1, lon1, lat2, lon2) -> int:
    R       = 6_371_000
    phi1    = math.radians(lat1)
    phi2    = math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return round(2 * R * math.asin(math.sqrt(a)))


def verificar_dentro_do_evento(lat, lon):
    distancia = calcular_distancia_metros(lat, lon, EVENTO_LAT, EVENTO_LON)
    return distancia <= RAIO_MAXIMO_METROS, distancia


def obter_localizacao():
    try:
        from streamlit_js_eval import get_geolocation
        return get_geolocation()
    except ImportError:
        st.error("❌ Pacote não encontrado. Execute: pip install streamlit-js-eval")
        return None


# ══════════════════════════════════════════════════════════════════
# TURNO ATIVO
# ══════════════════════════════════════════════════════════════════

def get_turno_ativo():
    agora = datetime.now(FUSO)
    for chave, turno in TURNOS.items():
        if turno["inicio"] <= agora <= turno["fim"]:
            return chave, turno["label"]
    return None, None


# ══════════════════════════════════════════════════════════════════
# HELPERS DE PARTICIPANTES
# ══════════════════════════════════════════════════════════════════

def get_iniciais(nome):
    return ' '.join(palavra[0].upper() for palavra in nome.split())


def get_ies_list():
    _, lista_evento = ler_dados_sheets()
    lista_evento['ies_completo'] = (
        lista_evento['co_ies'].astype(int).astype(str) + ' - ' + lista_evento['no_ies']
    )
    return lista_evento['ies_completo'].unique()


def get_participantes_ies(ies_selecionada):
    _, lista_evento = ler_dados_sheets()
    co_ies        = int(ies_selecionada.split(' - ')[0])
    participantes = lista_evento[
        lista_evento['co_ies'].astype(int) == co_ies
    ]['no_pessoa_fisica'].tolist()

    if 'mapeamento_nomes' not in st.session_state:
        st.session_state.mapeamento_nomes = {}

    participantes_iniciais = []
    for nome in participantes:
        iniciais      = get_iniciais(nome)
        base_iniciais = iniciais
        contador      = 1
        while (iniciais in st.session_state.mapeamento_nomes
               and st.session_state.mapeamento_nomes[iniciais] != nome):
            iniciais = f"{base_iniciais} ({contador})"
            contador += 1
        st.session_state.mapeamento_nomes[iniciais] = nome
        participantes_iniciais.append(iniciais)

    return participantes_iniciais


def verificar_presenca_existente(participante, turno_col) -> bool:
    presencas, _ = ler_dados_sheets()
    registro = presencas[presencas['nome_participante'] == participante]
    if registro.empty:
        return False
    return celula_preenchida(registro.iloc[0].get(turno_col))


def mostrar_historico(participante):
    presencas, _ = ler_dados_sheets()
    registro = presencas[presencas['nome_participante'] == participante]
    if registro.empty:
        return
    row = registro.iloc[0]
    st.markdown("**Seu histórico de presenças:**")
    for col in COLUNAS_PRESENCA:
        valor = row.get(col, "") or ""
        icone = "✅" if celula_preenchida(valor) else "⬜"
        label = valor if celula_preenchida(valor) else "—"
        st.write(f"{icone} {TURNOS[col]['label']}: {label}")


# ══════════════════════════════════════════════════════════════════
# INTERFACE PRINCIPAL
# ══════════════════════════════════════════════════════════════════

def main():
    # st.image(Image.open('logo.png').resize((400, 200)))
    st.title("✅ Lista de Presença — XI ENCES")

    # Flag de proteção contra duplo clique
    if "registrando" not in st.session_state:
        st.session_state.registrando = False

    # ── PASSO 1: Turno ativo ──────────────────────────────────────
    turno_col, turno_label = get_turno_ativo()

    if not turno_col:
        st.warning("⏰ **Nenhum turno de presença está aberto no momento.**")
        st.markdown("#### Horários de registro:")
        for _, t in TURNOS.items():
            st.write(
                f"• **{t['label']}**: "
                f"{t['inicio'].strftime('%d/%m às %H:%M')} — "
                f"{t['fim'].strftime('%H:%M')}h"
            )
        return

    st.success(f"🟢 Turno aberto: **{turno_label}**")
    st.divider()

    # ── PASSO 2: Geolocalização ───────────────────────────────────
    st.markdown("### 📍 Verificação de localização")
    st.caption(
        f"O registro só é permitido a até **{RAIO_MAXIMO_METROS}m** do local do evento. "
        "Permita o acesso à sua localização quando solicitado."
    )

    loc = obter_localizacao()

    if loc is None:
        st.info("⏳ Aguardando permissão de localização...")
        return

    lat = loc.get("coords", {}).get("latitude")
    lon = loc.get("coords", {}).get("longitude")

    if lat is None or lon is None:
        st.error("❌ Não foi possível obter sua localização. Verifique se o GPS está ativo.")
        return

    dentro, distancia = verificar_dentro_do_evento(lat, lon)

    if not dentro:
        st.error(
            f"🚫 Você está a **{distancia}m** do evento. "
            f"O registro só é permitido a até {RAIO_MAXIMO_METROS}m."
        )
        return

    st.success(f"✅ Localização confirmada! Você está a **{distancia}m** do evento.")
    st.divider()

    # ── PASSO 3: IES ──────────────────────────────────────────────
    ies_options = ["Selecione uma IES..."] + list(get_ies_list())
    ies = st.selectbox("Selecione sua Instituição", options=ies_options)

    if ies == "Selecione uma IES...":
        return

    # ── PASSO 4: Participante ─────────────────────────────────────
    participantes_iniciais = get_participantes_ies(ies)

    if not participantes_iniciais:
        st.error("Nenhum participante encontrado para esta IES")
        return

    participante_iniciais = st.selectbox(
        "Selecione o Participante",
        options=["Selecione um participante..."] + participantes_iniciais
    )

    if participante_iniciais == "Selecione um participante...":
        return

    nome_completo = st.session_state.mapeamento_nomes[participante_iniciais]

    # ── PASSO 5: Verifica presença duplicada ──────────────────────
    if verificar_presenca_existente(nome_completo, turno_col):
        st.warning(
            f"⚠️ **{participante_iniciais}** já registrou presença para **{turno_label}**.\n\n"
            "Não é permitido registrar presença mais de uma vez no mesmo turno."
        )
        mostrar_historico(nome_completo)
        return

    # ── PASSO 6: Confirmar ────────────────────────────────────────
    st.divider()
    st.markdown("### 📋 Confirmação de Presença")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**Participante:** {participante_iniciais}")
        st.markdown(f"**IES:** {ies}")
    with col2:
        st.markdown(f"**Turno:** {turno_label}")
        st.markdown(f"**Localização:** ✅ {distancia}m do evento")

    st.markdown("")

    submitted = st.button(
        "✅ Confirmar Presença",
        type="primary",
        use_container_width=True,
        disabled=st.session_state.registrando
    )

    if submitted and not st.session_state.registrando:
        st.session_state.registrando = True

        # Verificação dupla (race condition entre UI e clique)
        if verificar_presenca_existente(nome_completo, turno_col):
            st.warning("⚠️ Presença já registrada para este turno.")
            mostrar_historico(nome_completo)
            st.session_state.registrando = False
            return

        with st.spinner("Registrando presença..."):
            sucesso = adicionar_presenca(ies, nome_completo, turno_col)

        st.session_state.registrando = False

        if sucesso:
            st.success("🎉 **Presença registrada com sucesso!**")
            st.balloons()
            time.sleep(2)
            st.rerun()
        else:
            st.error("Não foi possível registrar a presença. Por favor, tente novamente.")


if __name__ == "__main__":
    main()
