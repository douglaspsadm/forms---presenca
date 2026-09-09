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
        "inicio": datetime(2026, 9, 9, 8, 45, tzinfo=FUSO),
        "fim":    datetime(2026, 9, 9, 8, 49, tzinfo=FUSO),
    },
    "presenca_dia2_manha": {
        "label":  "📅 Dia 2 — Manhã",
        "inicio": datetime(2026, 9, 9,  8,  50, tzinfo=FUSO),
        "fim":    datetime(2026, 9, 9, 8, 54, tzinfo=FUSO),
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
    Retorna timestamp atual em formato que o Google Sheets NÃO interpreta como data.
    "04/11/2026 08:25" seria convertido para serial. "04-11-2026 08h25" não é.
    """
    return datetime.now(FUSO).strftime("%d-%m-%Y %Hh%M")


def celula_preenchida(valor) -> bool:
    """Retorna True se a célula tem um valor real (não vazio, não NaN)."""
    if valor is None:
        return False
    return pd.notna(valor) and str(valor).strip() not in ("", "nan", "NaN", "None")


# ══════════════════════════════════════════════════════════════════
# DECORATOR RETRY — idêntico ao original
# ══════════════════════════════════════════════════════════════════

def retry_sheets_operation(max_retries=3, initial_delay=1):
    """Decorator para operações do Google Sheets com retry e exponential backoff."""
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
                            raise ValueError("Received empty DataFrame")
                    elif isinstance(result, pd.DataFrame) and result.empty:
                        raise ValueError("Received empty DataFrame")
                    return result
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        st.warning(f"Tentativa {attempt + 1} falhou. Tentando novamente em {delay} segundos...")
                        time.sleep(delay)
                        st.cache_data.clear()
                        delay *= 2
            st.error(f"Todas as {max_retries} tentativas falharam. Último erro: {str(last_exception)}")
            raise last_exception
        return wrapper
    return decorator


# ══════════════════════════════════════════════════════════════════
# LEITURA — com cache (para UI)
# ══════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1)
@retry_sheets_operation(max_retries=3, initial_delay=1)
def ler_dados_sheets():
    """
    Lê as duas abas principais com cache de 1s.
    Idêntico ao original.
    """
    conn         = st.connection('gsheets', type=GSheetsConnection)
    presencas    = conn.read(worksheet="presencas",    usecols=list(range(6)))
    lista_evento = conn.read(worksheet="lista_evento", usecols=[0, 1, 2])
    return presencas, lista_evento


def garantir_colunas(df: pd.DataFrame) -> pd.DataFrame:
    """Garante que o DataFrame tenha todas as colunas esperadas."""
    for col in TODAS_COLUNAS:
        if col not in df.columns:
            df[col] = ""
    return df[TODAS_COLUNAS]  # força a ordem correta das colunas


# ══════════════════════════════════════════════════════════════════
# BACKUP
# ══════════════════════════════════════════════════════════════════

def fazer_backup(conn, df_atual: pd.DataFrame) -> bool:
    """
    Salva cópia de 'presencas' na aba 'backup' com timestamp.
    Nunca salva DF vazio para não apagar backup anterior válido.
    Falha no backup NÃO interrompe o registro.
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
# REGISTRO DE PRESENÇA — CORRIGIDO
# ══════════════════════════════════════════════════════════════════

@retry_sheets_operation(max_retries=3, initial_delay=1)
def adicionar_presenca(ies: str, participante: str, turno_col: str) -> bool:
    """
    Registra presença do participante no turno informado.

    LÓGICA CORRIGIDA:
    ─────────────────────────────────────────────────────────────────
    ANTES (bugado): sempre fazia pd.concat → sempre criava nova linha
                    → mesmo participante acumulava múltiplas linhas

    AGORA (correto):
      • Participante JÁ TEM LINHA na planilha?
          → UPDATE: preenche só a coluna do turno na linha existente
          → sem nova linha, sem duplicata
      • Participante NOVO?
          → INSERT: cria uma única linha nova com os dados

    Proteções adicionais:
      • Backup antes de qualquer alteração
      • Validação de integridade: DF final não pode ser menor que o lido
      • Verificação dupla de presença já registrada (evita race condition)
    ─────────────────────────────────────────────────────────────────
    """
    try:
        conn = st.connection('gsheets', type=GSheetsConnection)

        # Limpa cache para garantir leitura fresca
        st.cache_data.clear()
        presencas, _ = ler_dados_sheets()
        presencas = garantir_colunas(presencas)

        n_linhas_antes = len(presencas)
        agora          = timestamp_agora()

        # Verificação dupla — proteção contra race condition
        # (dois cliques rápidos antes do cache expirar)
        mask = presencas["nome_participante"] == participante
        if mask.any():
            valor_atual = presencas.loc[mask, turno_col].iloc[0]
            if celula_preenchida(valor_atual):
                # Já registrou neste turno — aborta silenciosamente
                return False

        # ── Backup antes de qualquer escrita ─────────────────────
        fazer_backup(conn, presencas)

        # ── INSERT ou UPDATE ──────────────────────────────────────
        if mask.any():
            # ✅ CASO 1: Participante JÁ TEM LINHA
            # Atualiza SOMENTE a coluna do turno ativo.
            # As outras colunas (outros turnos já registrados) ficam intactas.
            presencas.loc[mask, turno_col] = agora
            data_atualizada = presencas

        else:
            # ✅ CASO 2: Participante NOVO
            # Cria uma linha nova com todos os campos vazios exceto o turno ativo.
            nova_linha = {col: "" for col in TODAS_COLUNAS}
            nova_linha["nome_participante"] = participante
            nova_linha["nome_ies"]          = ies
            nova_linha[turno_col]           = agora

            data_atualizada = pd.concat(
                [presencas, pd.DataFrame([nova_linha])],
                ignore_index=True
            )

        # ── Validação de integridade ──────────────────────────────
        # DF final nunca pode ser MENOR que o lido (proteção contra perda de dados)
        if len(data_atualizada) < n_linhas_antes:
            raise ValueError(
                f"Abortado: DF ficou com {len(data_atualizada)} linhas "
                f"(era {n_linhas_antes}). Dados originais preservados."
            )

        # ── Salva ─────────────────────────────────────────────────
        conn.update(worksheet="presencas", data=data_atualizada)
        st.cache_data.clear()
        return True

    except Exception as e:
        st.error(f"Erro ao registrar presença: {str(e)}")
        return False


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
    """Retorna (chave, label) do primeiro turno aberto no momento."""
    agora = datetime.now(FUSO)
    for chave, turno in TURNOS.items():
        if turno["inicio"] <= agora <= turno["fim"]:
            return chave, turno["label"]
    return None, None


# ══════════════════════════════════════════════════════════════════
# HELPERS DE PARTICIPANTES — idênticos ao original
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
    """Retorna True se o participante já registrou presença neste turno."""
    presencas, _ = ler_dados_sheets()
    registro = presencas[presencas['nome_participante'] == participante]
    if registro.empty:
        return False
    return celula_preenchida(registro.iloc[0].get(turno_col))


def mostrar_historico(participante):
    """Mostra o histórico completo de presenças do participante."""
    presencas, _ = ler_dados_sheets()
    registro = presencas[presencas['nome_participante'] == participante]
    if registro.empty:
        return
    row = registro.iloc[0]
    st.markdown("**Seu histórico de presenças:**")
    for col in COLUNAS_PRESENCA:
        valor = row.get(col, "") or ""
        icone = "✅" if celula_preenchida(valor) else "⬜"
        st.write(f"{icone} {TURNOS[col]['label']}: {valor if celula_preenchida(valor) else '—'}")


# ══════════════════════════════════════════════════════════════════
# INTERFACE PRINCIPAL
# ══════════════════════════════════════════════════════════════════

def main():
    # st.image(Image.open('logo.png').resize((400, 200)))
    st.title("✅ Lista de Presença — XI ENCES")

    # Inicializa flag de proteção contra duplo clique
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
        "Permita o acesso à sua localização quando solicitado pelo browser."
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

    # ── Proteção contra duplo clique ─────────────────────────────
    # O botão fica desabilitado enquanto o registro está em andamento.
    # Isso impede que dois cliques rápidos gerem dois registros.
    submitted = st.button(
        "✅ Confirmar Presença",
        type="primary",
        use_container_width=True,
        disabled=st.session_state.registrando   # ← desabilita após 1º clique
    )

    if submitted and not st.session_state.registrando:
        st.session_state.registrando = True

        # Verificação dupla antes de salvar (proteção contra race condition)
        if verificar_presenca_existente(nome_completo, turno_col):
            st.warning("⚠️ Presença já registrada para este turno.")
            mostrar_historico(nome_completo)
            st.session_state.registrando = False
            return

        with st.spinner("Registrando presença..."):
            sucesso = adicionar_presenca(ies, nome_completo, turno_col)

        if sucesso:
            st.success("🎉 **Presença registrada com sucesso!**")
            st.balloons()
            st.cache_data.clear()
            st.session_state.registrando = False
            time.sleep(2)
            st.rerun()
        else:
            st.error("Não foi possível registrar a presença. Por favor, tente novamente.")
            st.cache_data.clear()
            st.session_state.registrando = False


if __name__ == "__main__":
    main()
