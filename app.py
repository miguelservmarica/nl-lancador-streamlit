"""
################################################
#
#  Lançamento de NLs.
#  
#  Miguel Santos Silva
#  miguel.servmarica@gmail.com 
#
################################################
# 
# Programa para realizar automaticamente o lançamento das NLs geradas
# pelo setor da DCLT para cobrança de Legalização de Imóveis.
#
# Qualquer dúvida entrar em contato com miguel.servmarica@gmail.com 
#
################################################
"""

import streamlit as st
import re
import time
import datetime as dt
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional
import io

# ====== PDF leitura ======
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except Exception:
    HAS_PDFPLUMBER = False
    try:
        from PyPDF2 import PdfReader
        HAS_PYPDF2 = True
    except Exception:
        HAS_PYPDF2 = False

# ====== Selenium ======
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service as ChromeService


# ------------------------------
# Modelo de dados da NL
# ------------------------------
@dataclass
class NLItem:
    """Representa um item para lançamento (um tributo)."""
    descricao: str
    valor_rs: str
    valor_ufima: str

@dataclass
class NLData:
    """Agrupa metadados e a lista de lançamentos extraídos."""
    processo_origem: str = ""
    numero_nl: str = ""
    cgm: str = ""
    matricula: str = ""
    itens: List[NLItem] = field(default_factory=list)


# ------------------------------
# Parser de PDF
# ------------------------------
class PDFParser:
    def read_text(self, pdf_file) -> str:
        """Lê todas as páginas do PDF e devolve um texto único."""
        if HAS_PDFPLUMBER:
            parts = []
            with pdfplumber.open(pdf_file) as pdf:
                for p in pdf.pages:
                    parts.append(p.extract_text() or "")
            return "\n".join(parts)
        elif HAS_PYPDF2:
            reader = PdfReader(pdf_file)
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            raise RuntimeError("Nenhuma biblioteca de PDF disponível (instale pdfplumber ou PyPDF2).")

    @staticmethod
    def _fix_numbers_glitches(s: str) -> str:
        """Corrige padrões quebrados que ocorrem ao extrair texto de PDF."""
        s = re.sub(r"(R\$\s*)(\d)\s(\d,\d{2})", r"\1\2\3", s)
        s = re.sub(r"(R\$\s*)(\d)\s(\d{2},\d{2})", r"\1\2\3", s)
        s = re.sub(r"(?<!\d)(\d)\s(\d{3},\d{2})(?!\d)", r"\1.\2", s)
        s = re.sub(r"(?<!\d)(\d)\s(\d\.\d{3},\d{2})", r"\1\2", s)
        s = re.sub(r"(?<!\d)(\d)\s\.(\d{3},\d{2})", r"\1.\2", s)
        s = re.sub(r"(?<!\d)(\d{1,3}(?:\.\d{3})*,\d{2})\s*R\$", r"R$ \1", s)
        s = re.sub(r"[ \t]+", " ", s)
        return s

    def _recorte_janela(self, texto: str) -> str:
        """Pega SOMENTE o trecho entre 'Valor da UFIMA Corrente' e 'Total Geral'."""
        s = self._fix_numbers_glitches(texto)
        REAL = r"\d{1,3}(?:\.\d{3})*,\d{2}"
        UF   = r"\d+(?:[.,]\d{1,5})"

        ini = re.search(r"Valor da UFIMA Corrente\s*:\s*R\$\s*" + REAL, s, flags=re.I)
        if not ini:
            ini = re.search(r"Valor da UFIMA Corrente\s*:\s*" + REAL + r"\s*R\$", s, flags=re.I)

        fim = None
        for m in re.finditer(r"Total Geral\s+R\$\s*" + REAL + r"\s+" + UF + r"\s*UFIMA(?:\(\s*s\s*\))?",
                             s, flags=re.I):
            fim = m

        if ini and fim:
            return s[ini.end(): fim.end()]
        return s

    def _parse_header_fields(self, text: str) -> Dict[str, str]:
        out = {"processo": "", "nl": "", "cgm": "", "matricula": ""}

        m_processo = re.search(r"Processo\s+de\s+Origem[:\s]*([\d\.\,]+)", text, re.IGNORECASE)
        if not m_processo:
            m_processo = re.search(r"PROCESSO\s+ADMINISTRATIVO\s*[:\s]*([\d\.\,]+)", text, re.IGNORECASE)
        if m_processo:
            out["processo"] = m_processo.group(1).strip()

        m_nl = re.search(r"N[ºo]\s+(\d+/\d{4})", text, re.IGNORECASE)
        if m_nl:
            out["nl"] = m_nl.group(1).strip()

        m_cgm_tag = re.search(r"CGM\s*[:]*", text, re.IGNORECASE)
        if m_cgm_tag:
            sub = text[m_cgm_tag.end(): m_cgm_tag.end() + 120]
            nums = re.findall(r"\d{4,}", sub)
            if nums:
                out["cgm"] = nums[-1].strip()

        m_mat_tag = re.search(r"MATRICULA\s+IM[ÓO]VEL\s*[:]*", text, re.IGNORECASE)
        if m_mat_tag:
            tail = text[m_mat_tag.end(): m_mat_tag.end() + 100]
            lines = [t.strip() for t in tail.splitlines() if t.strip()]
            if lines:
                candidatos = re.findall(r"\d{2,}", lines[0])
                if candidatos:
                    out["matricula"] = candidatos[-1]
        return out

    def _extract_tributos_only(self, window_text: str) -> List[Tuple[str, str, str]]:
        """Extrai apenas os tributos permitidos."""
        s = re.sub(r"Tributos para Lançamento\s+Valor em R\$\s+Valor em UFIMA\(s\)\s*", "", window_text, flags=re.I)
        s = re.sub(r"Descrição das Taxas de Obras\s+Valor em R\$\s+Valor em UFIMA\(s\)\s*", "", s, flags=re.I)
        s = self._fix_numbers_glitches(s)
        s = re.sub(r"\s+", " ", s)

        uf_tok = r"UFIMA(?:\(\s*s\s*\))?"
        REAL = r"\d{1,3}(?:\.\d{3})*,\d{2}"
        UF   = r"\d+(?:[.,]\d{1,5})"
        
        pat = re.compile(
            r"(?P<desc>(?:ISS\s*-\s*.+?|Taxa(?:s)?\s+de\s+Obras(?:\s*-\s*.+?)?))\s+"
            r"R\$\s*(?P<rs>" + REAL + r")\s+(?P<uf>" + UF + r")\s*" + uf_tok,
            flags=re.I
        )

        matches: List[Tuple[str, str, str, int]] = []
        for m in pat.finditer(s):
            desc = re.sub(r"\s{2,}", " ", m.group("desc")).strip(" -").strip()

            allow = (
                desc.lower().startswith("iss -") or
                desc.lower() == "taxas de obras" or
                desc.lower() == "taxa de obras - vistoria residencial" or
                desc.lower() == "taxa de obras - vistoria comercial" or
                desc.lower() == "taxas de obras - renovação de alvará"
            )
            if not allow:
                continue

            rs = "R$ " + re.sub(r"(\d)\s(\d{3},\d{2})", r"\1.\2", m.group("rs").replace(" ", ""))
            uf = m.group("uf").replace(".", ",") + " UFIMA(s)"
            matches.append((desc, rs, uf, m.start()))

        bykey: Dict[Tuple[str, str], Tuple[str, str, int]] = {}
        for desc, rs, uf, pos in matches:
            k = (desc.lower(), rs)
            cur = bykey.get(k)
            if (cur is None) or (len(uf) > len(cur[1])) or (len(uf) == len(cur[1]) and pos < cur[2]):
                bykey[k] = (desc, uf, pos)

        ordered = sorted([(pos, desc, rs, uf) for (desc_lower, rs), (desc, uf, pos) in bykey.items()],
                         key=lambda t: t[0])

        return [(desc, rs, uf) for _, desc, rs, uf in ordered]

    def parse(self, pdf_file) -> NLData:
        raw = self.read_text(pdf_file)
        hdr = self._parse_header_fields(raw)
        data = NLData(
            processo_origem=hdr["processo"],
            numero_nl=hdr["nl"],
            cgm=hdr["cgm"],
            matricula=hdr["matricula"],
        )

        janela = self._recorte_janela(raw)
        rows = self._extract_tributos_only(janela)
        data.itens = [NLItem(d, r, u) for (d, r, u) in rows]
        return data


# ------------------------------
# Robô Selenium
# ------------------------------
class ECidadeBot:
    PROC_MAP: Dict[str, str] = {
        'ISS - Mão de Obra': '24',
        'ISS - Demolição': '24',
        'ISS - Reforma': '24',
        'ISS - Responsável Técnico': '36',
        'Taxa de Obras - Vistoria Residencial': '103',
        'Taxa de Obras - Vistoria Comercial': '103',
        'Taxas de Obras': '28',
        'Taxas de Obras - Renovação de Alvará': '28',
    }

    def __init__(self, log_placeholder):
        self.log_area = log_placeholder
        self.driver: Optional[webdriver.Chrome] = None
        self.wait: Optional[WebDriverWait] = None
        self.actions: Optional[ActionChains] = None

    def log(self, msg: str):
        """Adiciona mensagem ao log do Streamlit."""
        if 'logs' not in st.session_state:
            st.session_state.logs = []
        st.session_state.logs.append(msg)
        self.log_area.text_area("📡 Log do Sistema", 
                                value="\n".join(st.session_state.logs), 
                                height=200, 
                                key=f"log_{len(st.session_state.logs)}")

    def start(self, headless: bool = False):
        """Inicia o Chrome."""
        self.log("🖥️ Iniciando Chrome com webdriver-manager…")
        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--no-sandbox")
        if headless:
            options.add_argument("--headless=new")
        service = ChromeService(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)
        self.wait = WebDriverWait(self.driver, 30)
        self.actions = ActionChains(self.driver)

    def login(self, usuario: str, senha: str):
        """Abre a tela de login e autentica."""
        self.log("🔐 Abrindo página de login…")
        self.driver.get("https://ecidade.marica.rj.gov.br/e-cidade/login.php")
        self.log("⌨️ Digitando usuário/senha e clicando em 'Entrar'…")
        self.wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="usu_login"]'))).send_keys(usuario)
        self.wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="usu_senha"]'))).send_keys(senha)
        self.wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="btnlogar"]'))).click()
        
        try:
            self.wait.until(EC.presence_of_element_located((By.ID, 'areas')))
            self.log("✅ Login realizado com sucesso.")
        except TimeoutException:
            raise RuntimeError("Falha no login: verifique usuário/senha.")

    def navegar_para_inclusao(self):
        """Navega até a tela de inclusão."""
        self.log("🧭 Navegando até: MENU → DB:TRIBUTÁRIO → Diversos → Procedimentos → Inclusão…")
        try:
            self.wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/div[2]/div[1]'))).click()
        except:
            self.log("[🔄] O Chrome abriu em branco, tentando recuperar…")
            self.driver.execute_script("window.open('https://ecidade.marica.rj.gov.br/e-cidade/login.php', '_blank');")
            abas = self.driver.window_handles
            self.driver.switch_to.window(abas[-1])
            self.driver.switch_to.window(abas[0])
            self.driver.close()
            self.driver.switch_to.window(abas[-1])
            self.wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/div[2]/div[1]'))).click()
        
        self.wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="areas"]/span[2]'))).click()
        self.wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="modulos"]/span[2]'))).click()
        time.sleep(0.2)
        self.wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="menu_id_32"]'))).click()
        time.sleep(0.2)
        self.wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="menu_id_2233"]'))).click()
        self.wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="menu_id_2232"]'))).click()
        time.sleep(1)
        self.driver.get("https://ecidade.marica.rj.gov.br/e-cidade/w/1/dvr3_diversos004.php")
        
        try:
            self.wait.until(EC.presence_of_element_located((By.ID, 'z_numcgm')))
            self.log("✅ Tela de Inclusão carregada.")
        except TimeoutException:
            raise RuntimeError("Não foi possível abrir a tela de Inclusão.")

    @staticmethod
    def _ajusta_vencimento(base: dt.date, dias: int) -> dt.date:
        """Retorna data de vencimento ajustada."""
        alvo = base + dt.timedelta(days=dias)
        if alvo.weekday() == 5:
            alvo += dt.timedelta(days=2)
        elif alvo.weekday() == 6:
            alvo += dt.timedelta(days=1)
        return alvo

    @staticmethod
    def _data_ddmmyyyy_sem_barra(d: dt.date) -> str:
        return f"{d.day:02d}{d.month:02d}{d.year:04d}"

    @staticmethod
    def _normaliza_valor_brasil(valor_rs: str) -> str:
        """Converte 'R$ 17.257,22' → '17257,22'."""
        sem = valor_rs.replace("R$", "").strip()
        sem_pontos = sem.replace(".", "")
        return sem_pontos

    @staticmethod
    def _procedencia_for(descricao: str, proc_map: Dict[str, str]) -> str:
        """Determina o código de procedência."""
        desc = descricao.strip().lower()
        for k, v in proc_map.items():
            if desc.startswith(k.strip().lower()):
                return v
        if "iss" in desc:
            if "ISS - Mão de Obra".lower() in (k.lower() for k in proc_map):
                return proc_map["ISS - Mão de Obra"]
        for k, v in proc_map.items():
            if k.strip().lower() in desc:
                return v
        if "taxa" in desc or "taxas de obras" in desc:
            return proc_map.get("Taxas de Obras", "")
        return ""

    def _tenta_aceitar_alerta(self):
        """Tenta aceitar alertas do navegador."""
        try:
            alert = self.driver.switch_to.alert
            txt = alert.text
            alert.accept()
            self.log(f"   ⚠️ Alerta do navegador aceito: '{txt}'")
        except Exception:
            pass

    def lancar(self, data: NLData):
        """Realiza os lançamentos."""
        self.log("🚀 Iniciando lançamentos…")
        for idx, item in enumerate(data.itens, start=1):
            self.log(f"━ Lançamento {idx}/{len(data.itens)}: {item.descricao}")

            campo_cgm = self.wait.until(EC.presence_of_element_located((By.ID, 'z_numcgm')))
            campo_cgm.clear()
            campo_cgm.send_keys(data.cgm)
            self.wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/form/input'))).click()
            time.sleep(0.3)

            proc_code = self._procedencia_for(item.descricao, self.PROC_MAP)
            if not proc_code:
                raise RuntimeError(f"Não foi possível determinar a procedência para: {item.descricao}")
            campo_proc = self.wait.until(EC.presence_of_element_located((By.ID, 'dv05_procdiver')))
            campo_proc.clear()
            campo_proc.send_keys(proc_code)

            hoje = dt.date.today()
            delta = 30 if proc_code == "24" else 20
            venc = self._ajusta_vencimento(hoje, delta)
            dt_txt = self._data_ddmmyyyy_sem_barra(venc)
            self.log(f"   📅 Vencimento calculado: {venc.strftime('%d/%m/%Y')}")
            campo_venc = self.wait.until(EC.presence_of_element_located((By.ID, 'dv05_privenc')))
            campo_venc.clear()
            campo_venc.send_keys(dt_txt)

            vhist = self._normaliza_valor_brasil(item.valor_rs)
            campo_vhist = self.wait.until(EC.presence_of_element_located((By.ID, 'dv05_vlrhis')))
            campo_vhist.clear()
            campo_vhist.send_keys(vhist)

            try:
                btn_calc = self.wait.until(EC.element_to_be_clickable((By.XPATH, '/html/body/form/fieldset/table/tbody/tr[5]/td/fieldset/table/tbody/tr[3]/td[2]/input[2]')))
                btn_calc.click()
                time.sleep(0.8)
            except TimeoutException:
                self.log("   [ℹ️] Botão de cálculo não encontrado; prosseguindo.")

            obs = (
                f"Processo de Origem: {data.processo_origem}\n"
                f"NL: {data.numero_nl}\n"
                f"Matrícula do Imóvel: {data.matricula}\n"
                f"{item.descricao} | {item.valor_rs} | {item.valor_ufima}"
            )
            campo_obs = self.wait.until(EC.presence_of_element_located((By.ID, 'dv05_obs')))
            campo_obs.clear()
            campo_obs.send_keys(obs)

            self.wait.until(EC.element_to_be_clickable((By.ID, 'db_opcao'))).click()
            time.sleep(0.6)
            self.actions.send_keys(Keys.ENTER).perform()
            time.sleep(0.6)
            self._tenta_aceitar_alerta()

            try:
                self.wait.until(EC.presence_of_element_located((By.ID, 'z_numcgm')))
            except TimeoutException:
                self._tenta_aceitar_alerta()

            self.log(f"   ✅ Lançamento {idx} finalizado.")

        self.log("🎉 Todos os lançamentos da NL foram realizados com sucesso!")


# ------------------------------
# Interface Streamlit
# ------------------------------
def main():
    st.set_page_config(
        page_title="NL → E-Cidade | Lançador Automático",
        page_icon="🧾",
        layout="wide"
    )

    st.title("🧾 NL → E-Cidade (Maricá)")
    st.markdown("**Sistema de Lançamento Automático de Notificações de Lançamento**")
    st.markdown("---")

    # Inicializar session_state
    if 'data_atual' not in st.session_state:
        st.session_state.data_atual = None
    if 'logs' not in st.session_state:
        st.session_state.logs = []

    # Formulário de credenciais
    col1, col2 = st.columns(2)
    with col1:
        usuario = st.text_input("👤 Usuário", placeholder="Digite seu usuário do E-Cidade")
    with col2:
        senha = st.text_input("🔒 Senha", type="password", placeholder="Digite sua senha")

    st.markdown("---")

    # Upload do PDF
    st.subheader("📄 Upload da Notificação de Lançamento (PDF)")
    pdf_file = st.file_uploader("Selecione o arquivo PDF da NL", type=['pdf'])

    if pdf_file is not None:
        if st.button("📥 Importar & Extrair Dados", type="primary"):
            with st.spinner("🔄 Processando PDF..."):
                try:
                    parser = PDFParser()
                    data = parser.parse(pdf_file)
                    st.session_state.data_atual = data
                    st.success("✅ Extração concluída com sucesso!")
                    st.info(f"🔍 {len(data.itens)} lançamento(s) encontrados na NL {data.numero_nl}")
                except Exception as e:
                    st.error(f"❌ Erro ao processar o PDF: {e}")
                    return

    # Exibir dados extraídos
    if st.session_state.data_atual is not None:
        data = st.session_state.data_atual
        
        st.markdown("---")
        st.subheader("📋 Dados Extraídos")
        
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Processo", data.processo_origem)
        with col2:
            st.metric("NL", data.numero_nl)
        with col3:
            st.metric("CGM", data.cgm)
        with col4:
            st.metric("Matrícula", data.matricula)

        st.markdown("### 📊 Lançamentos a serem realizados:")
        
        # Criar texto editável
        texto_editavel = f"""Processo de Origem: {data.processo_origem}
NL: {data.numero_nl}
CGM do Sujeito Passivo: {data.cgm}
Matrícula do Imóvel: {data.matricula}

Lançamentos:
"""
        for item in data.itens:
            texto_editavel += f"{item.descricao} | {item.valor_rs} | {item.valor_ufima}\n"

        texto_editado = st.text_area(
            "✏️ Você pode editar os dados abaixo antes de lançar:",
            value=texto_editavel,
            height=300
        )

        st.info("💡 Edite o texto acima se precisar corrigir alguma informação antes do lançamento.")

        # Botão de lançamento
        st.markdown("---")
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            if st.button("🚀 Realizar Lançamento no E-Cidade", type="primary", use_container_width=True):
                if not usuario or not senha:
                    st.error("⚠️ Por favor, informe usuário e senha!")
                    return

                # Parse do texto editado
                try:
                    data_editada = parse_texto_editado(texto_editado)
                except Exception as e:
                    st.error(f"❌ Erro ao processar o texto editado: {e}")
                    return

                # Confirmação
                if st.session_state.get('confirmar_lancamento', False) == False:
                    st.warning(f"⚠️ Você está prestes a realizar {len(data_editada.itens)} lançamento(s) para a NL {data_editada.numero_nl}. Clique novamente para confirmar.")
                    st.session_state.confirmar_lancamento = True
                    st.stop()

                # Realizar lançamento
                st.session_state.confirmar_lancamento = False
                log_placeholder = st.empty()
                
                try:
                    bot = ECidadeBot(log_placeholder)
                    bot.start(headless=False)
                    bot.login(usuario, senha)
                    bot.navegar_para_inclusao()
                    bot.lancar(data_editada)
                    st.success("🎉 Lançamentos concluídos com sucesso!")
                except Exception as e:
                    st.error(f"❌ Erro durante a automação: {e}")
                finally:
                    time.sleep(3)
                    try:
                        if bot.driver:
                            bot.driver.quit()
                    except:
                        pass

    # Botão limpar
    if st.button("🧹 Limpar Tudo"):
        st.session_state.data_atual = None
        st.session_state.logs = []
        st.session_state.confirmar_lancamento = False
        st.rerun()


def parse_texto_editado(texto: str) -> NLData:
    """Reconstrói NLData a partir do texto editado."""
    RE_PROC = re.compile(r"Processo de Origem:\s*(.+)")
    RE_NL = re.compile(r"NL:\s*([0-9]+/[0-9]{4})")
    RE_CGM = re.compile(r"CGM do Sujeito Passivo:\s*([0-9\.]+)")
    RE_MAT = re.compile(r"Matrícula do Imóvel:\s*(.+)")
    RE_ITEM = re.compile(
        r"^(?P<desc>.+?)\s*\|\s*R\$\s*(?P<rs>\d{1,3}(?:\.\d{3})*,\d{2})\s*\|\s*(?P<uf>[\d\.,]+)\s*UFIMA\(s\)\s*$",
        re.IGNORECASE
    )

    proc = (RE_PROC.search(texto).group(1).strip() if RE_PROC.search(texto) else "")
    nl = (RE_NL.search(texto).group(1).strip() if RE_NL.search(texto) else "")
    cgm = (RE_CGM.search(texto).group(1).strip() if RE_CGM.search(texto) else "")
    cgm = re.sub(r"\D", "", cgm)
    mat = (RE_MAT.search(texto).group(1).strip() if RE_MAT.search(texto) else "")

    itens: List[NLItem] = []
    for ln in texto.splitlines():
        m = RE_ITEM.search(ln.strip())
        if m:
            desc = m.group("desc").strip()
            vrs = f"R$ {m.group('rs')}"
            vuf_raw = m.group("uf").replace(".", ",")
            vuf = f"{vuf_raw} UFIMA(s)"
            itens.append(NLItem(desc, vrs, vuf))

    if not (proc and nl and cgm and itens):
        raise RuntimeError("Dados incompletos. Verifique Processo, NL, CGM e lançamentos.")
    
    return NLData(processo_origem=proc, numero_nl=nl, cgm=cgm, matricula=mat, itens=itens)


if __name__ == "__main__":
    main()
