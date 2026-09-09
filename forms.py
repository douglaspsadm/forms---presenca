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
RAIO_MAXIMO_METROS = 4000  # raio em metros (300m é bem preciso para um prédio)

TURNOS = {
    "presenca_dia1_manha": {
        "label":  "📅 Dia 1 — Manhã",
        "inicio": datetime(2026, 9, 9,  8,  40, tzinfo=FUSO),
        "fim":    datetime(2026, 9, 9, 8, 44, tzinfo=FUSO),
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
# LEITURA PRINCIPAL — com cache (para UI)
# ══════════════════════════════════════════════════════════════════

@st.cache_data(ttl=1)
@retry_sheets_operation(max_retries=3, initial_delay=1)
def ler_dados_sheets():
    """
    Lê as duas abas principais.
    Idêntico ao original — usado para exibição na UI (tem cache).
    O retry já protege leituras normais.
    """
    conn         = st.connection('gsheets', type=GSheetsConnection)
    presencas    = conn.read(worksheet="presencas",    usecols=list(range(6)))
    lista_evento = conn.read(worksheet="lista_evento", usecols=[0, 1, 2])
    return presencas, lista_evento


# ══════════════════════════════════════════════════════════════════
# LEITURA FRESCA — sem cache (exclusiva para operações de escrita)
# ══════════════════════════════════════════════════════════════════

def ler_presencas_fresco(conn) -> pd.DataFrame:
    """
    Lê a aba 'presencas' direto do Sheets, sem cache.
    Chamada apenas dentro de adicionar_presenca(), antes do update.

    Tenta 3 vezes. Se todas falharem, retorna DF vazio estruturado
    e o chamador decidirá se usa o backup ou aceita vazio (1ª inscrição).
    """
    for tentativa in range(3):
        try:
            df = conn.read(worksheet="presencas", usecols=list(range(6)))
            # Garante que todas as colunas existam
            for col in TODAS_COLUNAS:
                if col not in df.columns:
                    df[col] = ""
            return df
        except Exception as exc:
            if tentativa < 2:
                espera = 2 ** tentativa   # 1s, 2s
                st.warning(f"⚠️ Leitura fresca falhou ({tentativa + 1}/3). Aguardando {espera}s...")
                time.sleep(espera)

    # Todas as tentativas falharam → retorna vazio estruturado
    return pd.DataFrame(columns=TODAS_COLUNAS)


# ══════════════════════════════════════════════════════════════════
# BACKUP
# ══════════════════════════════════════════════════════════════════

def fazer_backup(conn, df_atual: pd.DataFrame) -> bool:
    """
    Salva cópia da aba 'presencas' na aba 'backup' com timestamp.

    Regras de segurança:
    - NUNCA salva DF vazio (evita apagar o último backup válido).
    - Falha no backup NÃO interrompe o registro — apenas avisa.

    Retorna True se backup foi salvo, False caso contrário.
    """
    try:
        if df_atual.empty:
            # DF vazio pode ser falha de leitura — não apagar backup bom
            st.warning("⚠️ Backup ignorado: leitura retornou vazio (possível falha de rede).")
            return False

        df_backup             = df_atual.copy()
        df_backup["backup_em"] = datetime.now(FUSO).strftime("%d/%m/%Y %H:%M:%S")
        conn.update(worksheet="backup", data=df_backup)
        return True

    except Exception as exc:
        # Backup falhou, mas os dados originais ainda estão intactos no Sheets
        st.warning(f"⚠️ Backup não realizado (dados principais NÃO afetados): {exc}")
        return False


def recuperar_do_backup(conn) -> pd.DataFrame:
    """
    Tenta ler a aba 'backup' para recuperar dados em caso de leitura zerada.
    Remove a coluna 'backup_em' antes de retornar para não poluir 'presencas'.

    Usado APENAS quando ler_presencas_fresco() retornar vazio
    mas soubermos que já havia dados (impossível ser a 1ª inscrição).
    """
    try:
        df = conn.read(worksheet="backup", usecols=list(range(7)))  # 6 + backup_em
        if "backup_em" in df.columns:
            df = df.drop(columns=["backup_em"])
        for col in TODAS_COLUNAS:
            if col not in df.columns:
                df[col] = ""
        if not df.empty:
            st.warning("⚠️ Dados recuperados do backup por falha na leitura principal.")
        return df
    except Exception:
        return pd.DataFrame(columns=TODAS_COLUNAS)


# ══════════════════════════════════════════════════════════════════
# REGISTRO DE PRESENÇA
# ══════════════════════════════════════════════════════════════════

@retry_sheets_operation(max_retries=3, initial_delay=1)
def adicionar_presenca(ies: str, participante: str, turno_col: str) -> bool:
    """
    Registra a presença do participante no turno informado.

    Fluxo (mesma lógica do original, com backup e proteção contra zerado):

    1. Limpa cache
    2. Lê dados FRESCOS sem cache (3 tentativas)
    3. Se leitura voltou vazia E já existiam registros → recupera do backup
    4. Faz BACKUP do estado atual antes de qualquer alteração
    5. Concatena novo registro
    6. Valida integridade: DF final não pode ser MENOR que o lido
    7. Faz update (igual ao original)
    8. Limpa cache
    """
    try:
        conn = st.connection('gsheets', type=GSheetsConnection)
        st.cache_data.clear()

        # ── Passo 1: Leitura fresca (sem cache) ──────────────────
        presencas  = ler_presencas_fresco(conn)
        n_registros_lidos = len(presencas)

        # ── Passo 2: Recuperação do backup se necessário ──────────
        # Se voltou vazio MAS a leitura cacheada (UI) tinha dados,
        # é muito provável que seja falha de leitura, não planilha vazia.
        if presencas.empty:
            try:
                presencas_cache, _ = ler_dados_sheets()
                tinha_dados = not presencas_cache.empty
            except Exception:
                tinha_dados = False

            if tinha_dados:
                # Planilha deveria ter dados → tenta recuperar do backup
                presencas = recuperar_do_backup(conn)

        # ── Passo 3: Backup antes de alterar ─────────────────────
        fazer_backup(conn, presencas)

        # ── Passo 4: Monta novo registro ─────────────────────────
        agora = datetime.now(FUSO).strftime("%d/%m/%Y %H:%M:%S")

        novo_registro = pd.DataFrame({
            "nome_participante": [participante],
            "nome_ies":          [ies],
            "presenca_dia1_manha":  [""],
            "presenca_dia1_tarde":  [""],
            "presenca_dia2_manha":  [""],
            "presenca_dia2_tarde":  [""],
        })
        novo_registro[turno_col] = agora

        data_atualizada = pd.concat([presencas, novo_registro], ignore_index=True)

        # ── Passo 5: Validação de integridade ─────────────────────
        # O DF final deve ter pelo menos 1 linha a mais que o lido.
        # Se ficou menor, algo deu muito errado → aborta sem sobrescrever.
        if len(data_atualizada) <= n_registros_lidos and n_registros_lidos > 0:
            raise ValueError(
                f"Abortado por segurança: DF ficou com {len(data_atualizada)} linhas "
                f"após ter lido {n_registros_lidos}. Dados originais preservados."
            )

        # ── Passo 6: Salva ────────────────────────────────────────
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
    """Fórmula de Haversine — retorna distância em metros."""
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
    palavras = nome.split()
    return ' '.join(palavra[0].upper() for palavra in palavras)


def get_ies_list():
    _, lista_evento = ler_dados_sheets()
    lista_evento['ies_completo'] = (
        lista_evento['co_ies'].astype(int).astype(str) + ' - ' + lista_evento['no_ies']
    )
    return lista_evento['ies_completo'].unique()


def get_participantes_ies(ies_selecionada):
    _, lista_evento = ler_dados_sheets()
    co_ies       = int(ies_selecionada.split(' - ')[0])
    participantes = lista_evento[lista_evento['co_ies'].astype(int) == co_ies]['no_pessoa_fisica'].tolist()

    if 'mapeamento_nomes' not in st.session_state:
        st.session_state.mapeamento_nomes = {}

    participantes_iniciais = []
    for nome in participantes:
        iniciais      = get_iniciais(nome)
        base_iniciais = iniciais
        contador      = 1
        while iniciais in st.session_state.mapeamento_nomes and \
              st.session_state.mapeamento_nomes[iniciais] != nome:
            iniciais = f"{base_iniciais} ({contador})"
            contador += 1
        st.session_state.mapeamento_nomes[iniciais] = nome
        participantes_iniciais.append(iniciais)

    return participantes_iniciais


def verificar_presenca_existente(participante, turno_col):
    """Retorna True se o participante já registrou presença neste turno."""
    presencas, _ = ler_dados_sheets()
    registro = presencas[presencas['nome_participante'] == participante]
    if registro.empty:
        return False
    valor = registro.iloc[0].get(turno_col, None)
    return pd.notna(valor) and str(valor).strip() != ""


def mostrar_presenca_existente(participante):
    """Mostra o histórico de presenças do participante."""
    presencas, _ = ler_dados_sheets()
    registro = presencas[presencas['nome_participante'] == participante]
    if registro.empty:
        return
    row = registro.iloc[0]
    st.error("Você já possui registro de presença neste turno.")
    st.markdown("**Seu histórico de presenças:**")
    for col in COLUNAS_PRESENCA:
        valor = row.get(col, "") or "—"
        icone = "✅" if valor != "—" else "⬜"
        st.write(f"{icone} {TURNOS[col]['label']}: {valor}")
    st.warning("Não é permitido registrar presença mais de uma vez no mesmo turno.")


# ══════════════════════════════════════════════════════════════════
# INTERFACE PRINCIPAL
# ══════════════════════════════════════════════════════════════════

def main():
    # st.image(Image.open('logo.png').resize((400, 200)))
    st.title("✅ Lista de Presença — XI ENCES")

    # ── PASSO 1: Turno ativo ──────────────────────────────────────
    turno_col, turno_label = get_turno_ativo()

    if not turno_col:
        st.warning("⏰ **Nenhum turno de presença está aberto no momento.**")
        st.markdown("#### Horários de registro:")
        for _, t in TURNOS.items():
            st.write(f"• **{t['label']}**: {t['inicio'].strftime('%d/%m às %H:%M')} — {t['fim'].strftime('%H:%M')}h")
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

    if ies != "Selecione uma IES...":

        # ── PASSO 4: Participante ─────────────────────────────────
        participantes_iniciais = get_participantes_ies(ies)

        if participantes_iniciais:
            participante_iniciais = st.selectbox(
                "Selecione o Participante",
                options=["Selecione um participante..."] + participantes_iniciais
            )

            if participante_iniciais != "Selecione um participante...":
                nome_completo = st.session_state.mapeamento_nomes[participante_iniciais]

                # ── PASSO 5: Presença duplicada ───────────────────
                if verificar_presenca_existente(nome_completo, turno_col):
                    mostrar_presenca_existente(nome_completo)
                    return

                # ── PASSO 6: Confirmar e registrar ────────────────
                st.divider()
                st.markdown("### 📋 Confirmação de Presença")

                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**Participante:** {participante_iniciais}")
                    st.markdown(f"**IES:** {ies}")
                with col2:
                    st.markdown(f"**Turno:** {turno_label}")
                    st.markdown(f"**Localização:** ✅ {distancia}m do evento")

                submitted = st.button("✅ Confirmar Presença", type="primary", use_container_width=True)

                if submitted:
                    # Verificação dupla antes de salvar
                    if verificar_presenca_existente(nome_completo, turno_col):
                        mostrar_presenca_existente(nome_completo)
                        return

                    if adicionar_presenca(ies, nome_completo, turno_col):
                        st.success("🎉 **Presença registrada com sucesso!**")
                        st.balloons()
                        st.cache_data.clear()
                        time.sleep(2)
                        st.rerun()
                    else:
                        st.error("Não foi possível registrar a presença. Por favor, tente novamente.")
                        st.cache_data.clear()
        else:
            st.error("Nenhum participante encontrado para esta IES")


if __name__ == "__main__":
    main()
