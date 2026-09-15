"""EventUploader: sends the clan's tournament results from the game to the site.

Everyday use:
1. The Chrome with Total Battle is open; click "Conectar ao jogo".
2. In the game open the Journal and, on the tournament result, "Show details".
3. The tournament shows up in the list with its ranking. Check it and click
   "Enviar para o site". The site registers the tournament (or finds it, if it
   was already sent), and the review page opens in the browser.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
import webbrowser
from datetime import datetime, timezone
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

import api  # noqa: E402
import capture  # noqa: E402
import journal  # noqa: E402
from settings import Settings  # noqa: E402
from version import APP_VERSION  # noqa: E402

GAME_URL = "https://totalbattle.com/en/"
CHROME_PROFILE = os.path.expanduser(r"~\.total_battle_browser_profile")

HOW_TO = (
    "1. Conecte ao jogo (o Chrome com o Total Battle precisa estar aberto com depuração).\n"
    "2. No jogo, abra o Journal e clique em \"Show details\" no resultado do torneio.\n"
    "3. Confira o ranking abaixo e clique em Enviar para o site."
)


def type_of(key: Optional[str]) -> str:
    """The tournament type in a key: `1024` in `1024:1`. The catalogue is kept by type,
    because the game changes the rest of the key from one run to the next."""
    return (key or "").split(":")[0]


def fmt(n: Optional[int]) -> str:
    return "" if n is None else f"{n:,}".replace(",", ".")


def parse_utc(text: str) -> Optional[str]:
    """'12/09/2026 17:00' or '2026-09-12 17:00' -> ISO UTC, or None."""
    text = text.strip()
    for pattern in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


def find_chrome() -> Optional[str]:
    local = os.environ.get("LOCALAPPDATA", "")
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ):
        if path and os.path.exists(path):
            return path
    return None


class UploaderApp(tk.Tk):
    def __init__(self, settings: Optional[Settings] = None) -> None:
        super().__init__()
        self.title(f"EventUploader {APP_VERSION} - Torneios do jogo para o site")
        self.geometry("1180x820")
        self.minsize(960, 660)

        self.settings = settings or Settings()
        self.collector = journal.JournalCollector()
        self.capture: Optional[capture.CdpCapture] = None
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self.known: Dict[str, dict] = {}
        self.listed: List[journal.Tournament] = []
        self.current: Optional[journal.Tournament] = None
        self.names: Dict[str, str] = {}
        self.dirty = False
        self.busy = False

        self._build()
        self.site_entry.insert(0, self.settings.site_url)
        self.token_entry.insert(0, self.settings.get_token())
        self.after(300, self._drain)
        self.after(600, self._poll_cdp)
        self.after(1000, self._refresh_list_if_dirty)
        if self.settings.site_url and self.settings.get_token():
            self.after(200, lambda: self._test_site(quiet=True))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}

        site = ttk.LabelFrame(self, text="Site")
        site.pack(fill="x", **pad)
        ttk.Label(site, text="Endereço").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.site_entry = ttk.Entry(site, width=42)
        self.site_entry.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(site, text="Token de API").grid(row=0, column=2, sticky="w", padx=6)
        self.token_entry = ttk.Entry(site, width=40, show="•")
        self.token_entry.grid(row=0, column=3, sticky="w", padx=6)
        ttk.Button(site, text="Salvar e testar", command=self._test_site).grid(row=0, column=4, padx=6)
        self.site_status = ttk.Label(site, text="Não testado", foreground="#6b7280")
        self.site_status.grid(row=0, column=5, sticky="w", padx=6)

        game = ttk.LabelFrame(self, text="Jogo")
        game.pack(fill="x", **pad)
        self.cdp_label = ttk.Label(game, text="Chrome: verificando...", width=30)
        self.cdp_label.pack(side="left", padx=6, pady=4)
        ttk.Button(game, text="Abrir Chrome", command=self._open_chrome).pack(side="left", padx=4)
        self.connect_btn = ttk.Button(game, text="Conectar ao jogo", command=self._toggle_capture)
        self.connect_btn.pack(side="left", padx=4)
        ttk.Button(game, text="Abrir gravação...", command=self._load_recording).pack(side="left", padx=4)
        self.packets_label = ttk.Label(game, text="")
        self.packets_label.pack(side="right", padx=8)

        ttk.Label(self, text=HOW_TO, justify="left").pack(anchor="w", padx=14, pady=(2, 4))

        body = ttk.PanedWindow(self, orient="vertical")
        body.pack(fill="both", expand=True, **pad)

        top = ttk.LabelFrame(body, text="Torneios encontrados")
        body.add(top, weight=1)
        cols = ("fim", "tipo", "nome", "jogadores", "sem_nome", "situacao")
        self.list_tree = ttk.Treeview(top, columns=cols, show="headings", height=6, selectmode="browse")
        for c, label, w in zip(cols, ("Terminou (UTC)", "Tipo", "Nome no site", "Jogadores", "Sem nome", "Situação"),
                               (130, 80, 300, 90, 80, 360)):
            self.list_tree.heading(c, text=label)
            self.list_tree.column(c, width=w, anchor="w")
        self.list_tree.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(top, orient="vertical", command=self.list_tree.yview)
        sb.pack(side="right", fill="y")
        self.list_tree.configure(yscrollcommand=sb.set)
        self.list_tree.bind("<<TreeviewSelect>>", self._on_select)

        bottom = ttk.LabelFrame(body, text="Ranking do torneio selecionado")
        body.add(bottom, weight=3)

        form = ttk.Frame(bottom)
        form.pack(fill="x", padx=6, pady=4)
        ttk.Label(form, text="Nome do torneio").grid(row=0, column=0, sticky="w")
        self.name_entry = ttk.Entry(form, width=40)
        self.name_entry.grid(row=0, column=1, sticky="w", padx=6)
        self.name_hint = ttk.Label(form, text="", foreground="#6b7280")
        self.name_hint.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(form, text="Terminou em (UTC)").grid(row=0, column=2, sticky="w", padx=(18, 0))
        self.ended_entry = ttk.Entry(form, width=18)
        self.ended_entry.grid(row=0, column=3, sticky="w", padx=6)
        self.send_btn = ttk.Button(form, text="Enviar para o site", command=self._send, state="disabled")
        self.send_btn.grid(row=0, column=4, padx=(18, 4))
        self.review_btn = ttk.Button(form, text="Abrir revisão no site", command=self._open_review, state="disabled")
        self.review_btn.grid(row=0, column=5, padx=4)

        self.check_text = tk.Text(bottom, height=6, wrap="word")
        self.check_text.pack(fill="x", padx=6, pady=4)

        rank_frame = ttk.Frame(bottom)
        rank_frame.pack(fill="both", expand=True, padx=6, pady=4)
        rcols = ("pos", "nome", "pontos", "poder", "id")
        self.rank_tree = ttk.Treeview(rank_frame, columns=rcols, show="headings")
        for c, label, w, anchor in zip(rcols, ("#", "Jogador", "Pontos", "Poder", "Id no jogo"),
                                       (50, 300, 170, 170, 150), ("e", "w", "e", "e", "w")):
            self.rank_tree.heading(c, text=label)
            self.rank_tree.column(c, width=w, anchor=anchor)
        self.rank_tree.pack(fill="both", expand=True, side="left")
        sb2 = ttk.Scrollbar(rank_frame, orient="vertical", command=self.rank_tree.yview)
        sb2.pack(side="right", fill="y")
        self.rank_tree.configure(yscrollcommand=sb2.set)
        self.rank_tree.tag_configure("unnamed", foreground="#b91c1c")

        self.status = ttk.Label(self, text="Pronto.")
        self.status.pack(fill="x", padx=12, pady=(0, 6))

    # --------------------------------------------------------------- utilities
    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _client(self) -> api.SiteClient:
        return api.SiteClient(self.site_entry.get(), self.token_entry.get(), APP_VERSION)

    def _in_thread(self, work, done) -> None:
        """Run a network call off the window thread and hand the result back."""
        def run():
            try:
                result, error = work(), None
            except api.ApiError as exc:
                result, error = None, exc
            except Exception as exc:  # anything unexpected becomes a message, not a frozen window
                result, error = None, api.ApiError(0, str(exc))
            self.inbox.put({"__done__": (done, result, error)})
        threading.Thread(target=run, daemon=True).start()

    def _drain(self) -> None:
        while True:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                break
            if "__done__" in item:
                done, result, error = item["__done__"]
                done(result, error)
            elif "__status__" in item:
                self._set_status(item["__status__"])
            elif "__cdp__" in item:
                available, game = item["__cdp__"]
                text = "Chrome: jogo encontrado" if game else ("Chrome: sem aba do jogo" if available else "Chrome: sem depuração")
                self.cdp_label.configure(text=text, foreground="#15803d" if game else "#b91c1c")
        self.after(300, self._drain)

    def _poll_cdp(self) -> None:
        port = self.settings.cdp_port

        def probe():
            probe_capture = capture.CdpCapture(port=port, out_dir=None)
            available = probe_capture.cdp_available()
            game = available and probe_capture.find_game_tab() is not None
            self.inbox.put({"__cdp__": (available, game)})
        threading.Thread(target=probe, daemon=True).start()
        self.after(4000, self._poll_cdp)

    # --------------------------------------------------------------------- site
    def _test_site(self, quiet: bool = False) -> None:
        self.settings.site_url = api.normalize_site_url(self.site_entry.get())
        self.site_entry.delete(0, "end")
        self.site_entry.insert(0, self.settings.site_url)
        self.settings.set_token(self.token_entry.get())
        self.settings.save()
        self.site_status.configure(text="Testando...", foreground="#6b7280")
        client = self._client()

        def work():
            return client.me(), client.known_tournaments()

        def done(result, error):
            if error is not None:
                self.site_status.configure(text="Falhou", foreground="#b91c1c")
                if not quiet:
                    messagebox.showerror("Site", error.describe())
                return
            me, known = result
            self.known = {str(k["game_type"]): k for k in known}
            name = (me.get("user") or {}).get("name") or "?"
            self.site_status.configure(text=f"Conectado como {name}", foreground="#15803d")
            if self.settings.token_in_file and not quiet:
                messagebox.showwarning("Token", "O Cofre de Credenciais do Windows não está disponível: o token foi guardado no arquivo de configuração.")
            self.dirty = True
            self._show_current()
        self._in_thread(work, done)

    # --------------------------------------------------------------------- game
    def _open_chrome(self) -> None:
        if capture.CdpCapture(port=self.settings.cdp_port, out_dir=None).cdp_available():
            messagebox.showinfo("Chrome", "O Chrome já está aberto com depuração.")
            return
        exe = find_chrome()
        if not exe:
            messagebox.showerror("Chrome", "Chrome não encontrado. Abra-o com --remote-debugging-port=9222.")
            return
        os.makedirs(CHROME_PROFILE, exist_ok=True)
        subprocess.Popen([
            exe, f"--remote-debugging-port={self.settings.cdp_port}", "--remote-allow-origins=*",
            f"--user-data-dir={CHROME_PROFILE}", "--no-first-run", "--no-default-browser-check",
            "--start-maximized", GAME_URL,
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._set_status("Chrome aberto. Entre no jogo e conecte.")

    def _toggle_capture(self) -> None:
        if self.capture and self.capture.is_running:
            self.capture.stop()
            self.capture = None
            self.connect_btn.configure(text="Conectar ao jogo")
            self._set_status("Desconectado do jogo.")
            return

        self.capture = capture.CdpCapture(
            port=self.settings.cdp_port,
            out_dir=None,
            on_status=lambda m: self.inbox.put({"__status__": m}),
            on_record=self._on_record,
        )
        if self.capture.start():
            self.connect_btn.configure(text="Desconectar")
            self._set_status("Conectado. Abra o Journal e o \"Show details\" do torneio.")
        else:
            self.capture = None

    def _on_record(self, record: dict) -> None:
        """Called on the capture thread."""
        if self.collector.feed_record(record):
            self.dirty = True

    def _load_recording(self) -> None:
        path = filedialog.askopenfilename(
            initialdir=os.path.join(BASE_DIR, "gravacoes"), filetypes=[("Gravações", "*.jsonl"), ("Todos", "*.*")]
        )
        if not path:
            return
        self._set_status("Lendo gravação...")

        def work():
            for record in capture.load_recording(path):
                self.collector.feed_record(record)
            return True

        def done(_result, error):
            if error is not None:
                messagebox.showerror("Gravação", error.describe())
            self.dirty = True
            self._set_status(f"Gravação lida: {os.path.basename(path)}")
        self._in_thread(work, done)

    # ------------------------------------------------------------------- list
    def _refresh_list_if_dirty(self) -> None:
        if self.dirty:
            self.dirty = False
            self._refresh_list()
        stats = self.capture.stats if self.capture else None
        self.packets_label.configure(
            text=f"{len(self.collector.profiles)} perfis · {self.collector.packets} pacotes lidos"
            + (f" · {stats.get('WS_IN', 0)} ws" if stats else "")
        )
        self.after(1000, self._refresh_list_if_dirty)

    def _situation(self, t: journal.Tournament) -> str:
        sent = self.settings.sent_info(t.result_uid)
        if not t.has_ranking:
            return "Abra o \"Show details\" deste torneio"
        if sent:
            return f"Enviado · evento #{sent.get('event_number', '?')}"
        if t.unnamed:
            return "Faltam nomes: reabra o \"Show details\""
        return "Pronto para enviar"

    def _site_name(self, t: journal.Tournament) -> str:
        if t.result_uid in self.names:
            return self.names[t.result_uid]
        known = self.known.get(type_of(t.tournament_key))
        return known["name"] if known and known.get("name") else ""

    def _refresh_list(self) -> None:
        selected = self.current.result_uid if self.current else None
        self.listed = self.collector.tournaments()
        self.list_tree.delete(*self.list_tree.get_children())
        for t in self.listed:
            self.list_tree.insert("", "end", iid=t.result_uid, values=(
                t.ended_at_text, t.tournament_key or "?", self._site_name(t),
                len(t.rows) or "", t.unnamed or "", self._situation(t),
            ))
        if selected and self.list_tree.exists(selected):
            self.list_tree.selection_set(selected)
            self.current = next(t for t in self.listed if t.result_uid == selected)
            self._show_current(keep_form=True)
        elif self.listed and self.listed[0].has_ranking and not selected:
            self.list_tree.selection_set(self.listed[0].result_uid)

    def _on_select(self, _event=None) -> None:
        selection = self.list_tree.selection()
        if not selection:
            return
        # The list is redrawn whenever new packets arrive and re-selects the same
        # row: that must not wipe a name or date being typed.
        if self.current is not None and self.current.result_uid == selection[0]:
            return
        if self.current is not None:
            self.names[self.current.result_uid] = self.name_entry.get()
        self.current = next((t for t in self.listed if t.result_uid == selection[0]), None)
        self._show_current()

    def _show_current(self, keep_form: bool = False) -> None:
        t = self.current
        if t is None:
            return

        if not keep_form:
            self.name_entry.delete(0, "end")
            self.name_entry.insert(0, self._site_name(t))
            self.ended_entry.delete(0, "end")
            self.ended_entry.insert(0, t.ended_at_text)
        known = self.known.get(type_of(t.tournament_key))
        if known and known.get("name"):
            rewards = ", ".join(f"{fmt(r['quantity'])} {r['item_name']}" for r in known.get("rewards", []))
            days = known.get("duration_days") or 1
            self.name_hint.configure(
                text=f"No catálogo do site · dura {days} dia(s) · prêmios herdados: {rewards or 'nenhum ainda'}")
        else:
            self.name_hint.configure(
                text="Tipo sem nome no catálogo: informe o nome (ou rode o mapeador) e cadastre os prêmios na revisão.")

        self.check_text.delete("1.0", "end")
        if t.has_ranking:
            for ok, text in journal.checks(t):
                self.check_text.insert("end", ("[OK]    " if ok else "[ATENÇÃO] ") + text + "\n")
        else:
            self.check_text.insert("end", "O ranking deste torneio ainda não chegou: clique em \"Show details\" nele, no Journal.\n")

        self.rank_tree.delete(*self.rank_tree.get_children())
        for row in t.rows:
            self.rank_tree.insert("", "end", values=(row.position, row.display_name, fmt(row.points), fmt(row.power), row.player_id),
                                  tags=() if row.name else ("unnamed",))

        sent = self.settings.sent_info(t.result_uid)
        self.send_btn.configure(state="normal" if t.has_ranking and not self.busy else "disabled",
                                text="Reenviar para o site" if sent else "Enviar para o site")
        self.review_btn.configure(state="normal" if sent else "disabled")

    # ------------------------------------------------------------------- send
    def _send(self) -> None:
        t = self.current
        if t is None or not t.has_ranking or self.busy:
            return

        ended_text = self.ended_entry.get().strip()
        if t.ended_at is not None and ended_text == t.ended_at_text:
            # Untouched: send the exact moment the game gave, seconds included.
            ended_iso = t.ended_at_iso
        else:
            ended_iso = parse_utc(ended_text) if ended_text else None
        if ended_text and ended_iso is None:
            messagebox.showwarning("Data", "Use o formato 12/09/2026 17:00 (UTC).")
            return
        if ended_iso is None and not messagebox.askyesno(
            "Data desconhecida", "A data de término não é conhecida. O site vai usar a data de agora. Enviar assim mesmo?"
        ):
            return
        if t.unnamed and not messagebox.askyesno(
            "Jogadores sem nome",
            f"{t.unnamed} jogador(es) estão sem nome e irão como 'id:...'. O site ainda casa pelo id do jogo, "
            "mas é melhor reabrir o \"Show details\". Enviar assim mesmo?",
        ):
            return

        name = self.name_entry.get().strip()
        self.names[t.result_uid] = name
        payload = journal.build_payload(t, name, ended_iso, APP_VERSION)
        client = self._client()
        self.busy = True
        self.send_btn.configure(state="disabled")
        self._set_status("Enviando...")

        def done(result, error):
            self.busy = False
            if error is not None:
                self._set_status("O envio falhou.")
                messagebox.showerror("Envio", error.describe())
                self._show_current(keep_form=True)
                return
            self.settings.mark_sent(t.result_uid, {
                "event_id": result.get("event_id"),
                "event_number": result.get("event_number"),
                "review_url": result.get("review_url"),
                "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            if t.tournament_key and result.get("event_name"):
                self.known.setdefault(type_of(t.tournament_key), {"game_type": type_of(t.tournament_key), "rewards": []})["name"] = result["event_name"]
            lines = [
                f"Evento #{result.get('event_number')} - {result.get('event_name')}",
                "Torneio cadastrado agora." if result.get("event_created") else "Torneio já existia no site.",
                "Ranking novo salvo como rascunho." if result.get("created") else "Ranking igual ao que já estava lá.",
                f"{result.get('linked', 0)} de {result.get('rows', 0)} jogadores ligados a membros.",
            ]
            if result.get("starts_at") and result.get("ends_at"):
                lines.append(f"Período: {result['starts_at'][:16].replace('T', ' ')} a {result['ends_at'][:16].replace('T', ' ')} UTC (ajustável na revisão).")
            members = result.get("members") or {}
            if members.get("applied"):
                lines.append(
                    f"Membros: {members.get('created', 0)} novo(s), {members.get('renamed', 0)} renomeado(s), "
                    f"{members.get('activated', 0)} reativado(s), {members.get('deactivated', 0)} marcado(s) como inativo(s)."
                )
                if members.get("ambiguous"):
                    lines.append("Nomes repetidos sem id, não vinculados: " + ", ".join(members["ambiguous"][:5]))
            elif members.get("reason") == "older":
                lines.append("Membros não alterados: já existe um torneio mais recente no site.")
            if result.get("rewards_copied"):
                lines.append(f"{result['rewards_copied']} prêmio(s) copiados do torneio anterior do mesmo tipo.")
            elif not result.get("rewards"):
                lines.append("Sem prêmios: cadastre-os na revisão antes de publicar.")
            if result.get("warnings"):
                lines += ["", "Avisos:"] + [f"- {w}" for w in result["warnings"]]
            self._set_status(f"Enviado: evento #{result.get('event_number')}.")
            self.dirty = True
            if messagebox.askyesno("Enviado", "\n".join(lines) + "\n\nAbrir a revisão no site?"):
                self._open_review()

        self._in_thread(lambda: client.send_tournament(payload), done)

    def _open_review(self) -> None:
        if self.current is None:
            return
        sent = self.settings.sent_info(self.current.result_uid)
        if sent and sent.get("review_url"):
            webbrowser.open(sent["review_url"])

    def _on_close(self) -> None:
        if self.capture and self.capture.is_running:
            self.capture.stop()
        self.destroy()


if __name__ == "__main__":
    UploaderApp().mainloop()
