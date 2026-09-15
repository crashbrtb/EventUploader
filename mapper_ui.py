"""The "Mapear torneios" tab: calibrate the Journal, walk it, send names to the site."""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Dict, List, Optional

import api
import capture
import journal
import mapper
from browser import Browser
from calibration import STEPS_BY_NAME, Calibration
from settings import Settings
from titles import Ocr
from version import APP_VERSION
from wizard import CalibrationWizard

HOW_TO = (
    "Antes de mapear: aperte F5 no jogo e espere carregar (assim o jogo busca de novo os rankings já abertos),\n"
    "deixe-o na tela principal - ou com o Journal aberto na página 1, se o botão do Journal não foi calibrado -\n"
    "e não mexa no jogo enquanto o mapeador trabalha. Ele abre cada resultado \"Your Clanmates' results in ...\",\n"
    "clica em Show details e anota qual tipo de torneio o jogo envia para aquele nome."
)


class MapperFrame(ttk.Frame):
    def __init__(self, master) -> None:
        super().__init__(master)
        self.settings = Settings()
        self.calibration = Calibration()
        self.ocr = Ocr()
        self.browser = Browser(port=self.settings.cdp_port)
        self.collector = journal.JournalCollector()
        self.capture: Optional[capture.CdpCapture] = None
        self.runner: Optional[mapper.JournalMapper] = None
        self.cards: List[mapper.MappedCard] = []
        self.known_names: Dict[str, str] = {}
        self.inbox: "queue.Queue[dict]" = queue.Queue()

        self._build()
        self.site_entry.insert(0, self.settings.site_url)
        self.token_entry.insert(0, self.settings.get_token())
        self._refresh_calibration_label()
        self.after(300, self._drain)

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}

        site = ttk.LabelFrame(self, text="Site")
        site.pack(fill="x", **pad)
        ttk.Label(site, text="Endereço").grid(row=0, column=0, padx=6, pady=4, sticky="w")
        self.site_entry = ttk.Entry(site, width=40)
        self.site_entry.grid(row=0, column=1, padx=6, sticky="w")
        ttk.Label(site, text="Token de API").grid(row=0, column=2, padx=6, sticky="w")
        self.token_entry = ttk.Entry(site, width=36, show="•")
        self.token_entry.grid(row=0, column=3, padx=6, sticky="w")
        ttk.Button(site, text="Salvar e testar", command=self._test_site).grid(row=0, column=4, padx=6)
        self.site_status = ttk.Label(site, text="Não testado", foreground="#6b7280")
        self.site_status.grid(row=0, column=5, padx=6, sticky="w")

        game = ttk.LabelFrame(self, text="Journal")
        game.pack(fill="x", **pad)
        ttk.Button(game, text="Calibrar Journal...", command=self._open_wizard).pack(side="left", padx=6, pady=4)
        self.calibration_label = ttk.Label(game, text="")
        self.calibration_label.pack(side="left", padx=8)

        options = ttk.LabelFrame(self, text="Opções")
        options.pack(fill="x", **pad)
        ttk.Label(options, text="Páginas no máximo").pack(side="left", padx=(6, 2), pady=4)
        self.pages_var = tk.IntVar(value=30)
        ttk.Spinbox(options, from_=1, to=200, width=5, textvariable=self.pages_var).pack(side="left", padx=(0, 12))
        self.open_journal_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Abrir o Journal automaticamente", variable=self.open_journal_var).pack(side="left", padx=6)
        self.reopen_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Abrir também torneios já mapeados", variable=self.reopen_var).pack(side="left", padx=6)
        self.send_rankings_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Enviar também os rankings abertos (cadastra os torneios no site)",
                        variable=self.send_rankings_var).pack(side="left", padx=6)

        ttk.Label(self, text=HOW_TO, justify="left", foreground="#374151").pack(anchor="w", padx=14, pady=(2, 4))

        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)
        self.start_btn = ttk.Button(actions, text="Iniciar mapeamento", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(actions, text="Parar", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.send_btn = ttk.Button(actions, text="Enviar ao catálogo do site", command=self._send, state="disabled")
        self.send_btn.pack(side="left", padx=16)
        self.status = ttk.Label(actions, text="Pronto.")
        self.status.pack(side="left", padx=8)

        body = ttk.PanedWindow(self, orient="vertical")
        body.pack(fill="both", expand=True, **pad)

        table = ttk.Frame(body)
        body.add(table, weight=3)
        cols = ("pagina", "card", "nome", "tipo", "fim", "icone", "situacao")
        self.tree = ttk.Treeview(table, columns=cols, show="headings")
        for c, label, w in zip(cols, ("Pág.", "Card", "Nome lido", "Tipo no jogo", "Terminou (UTC)", "Ícone", "Situação"),
                               (50, 50, 340, 110, 130, 60, 260)):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        self.log = tk.Text(body, height=10, wrap="word", font=("Consolas", 9))
        body.add(self.log, weight=1)

    # -------------------------------------------------------------- helpers
    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _write_log(self, text: str) -> None:
        self.log.insert("end", time.strftime("%H:%M:%S ") + text + "\n")
        self.log.see("end")

    def _refresh_calibration_label(self) -> None:
        missing = self.calibration.missing_steps()
        if missing:
            names = ", ".join(STEPS_BY_NAME[m].title for m in missing)
            self.calibration_label.configure(text=f"Falta calibrar: {names}", foreground="#b91c1c")
        else:
            self.calibration_label.configure(
                text=f"Calibrado ({self.calibration.cards_per_page} cards por página)", foreground="#15803d")

    def _client(self) -> api.SiteClient:
        return api.SiteClient(self.site_entry.get(), self.token_entry.get(), APP_VERSION)

    def _in_thread(self, work, done) -> None:
        def run():
            try:
                result, error = work(), None
            except api.ApiError as exc:
                result, error = None, exc.describe()
            except Exception as exc:
                result, error = None, str(exc)
            self.inbox.put({"done": (done, result, error)})
        threading.Thread(target=run, daemon=True).start()

    def _drain(self) -> None:
        while True:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                break
            if "done" in item:
                done, result, error = item["done"]
                done(result, error)
            elif "log" in item:
                self._write_log(item["log"])
            elif "card" in item:
                self._add_card(item["card"])
        self.after(300, self._drain)

    # ------------------------------------------------------------------ site
    def _test_site(self) -> None:
        self.settings.site_url = api.normalize_site_url(self.site_entry.get())
        self.settings.set_token(self.token_entry.get())
        self.settings.save()
        client = self._client()

        def done(result, error):
            if error:
                self.site_status.configure(text="Falhou", foreground="#b91c1c")
                messagebox.showerror("Site", error)
                return
            me, known = result
            self.known_names = {k["name"]: str(k["game_type"]) for k in known if k.get("name")}
            self.site_status.configure(text=f"Conectado como {me['user'].get('name')} · {len(self.known_names)} torneio(s) com nome",
                                       foreground="#15803d")
        self._in_thread(lambda: (client.me(), client.known_tournaments()), done)

    # --------------------------------------------------------------- journal
    def _open_wizard(self) -> None:
        def connect():
            if not self.browser.connected and not self.browser.connect():
                raise RuntimeError("Não achei a aba do Total Battle no Chrome com depuração (porta 9222).")
            return True

        def done(_result, error):
            if error:
                messagebox.showerror("Calibração", error)
                return
            CalibrationWizard(self.winfo_toplevel(), self.browser, self.calibration, self.ocr,
                              on_done=self._refresh_calibration_label)
        self._in_thread(connect, done)

    def _start(self) -> None:
        if self.calibration.missing_steps():
            messagebox.showwarning("Calibração", "Calibre o Journal antes de mapear.")
            return
        if not self.ocr.available():
            messagebox.showerror("OCR", self.ocr.error)
            return
        self.cards = []
        self.tree.delete(*self.tree.get_children())
        self.collector.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.send_btn.configure(state="disabled")
        self._set_status("Mapeando... não mexa no jogo.")

        options = mapper.MapperOptions(
            max_pages=max(1, int(self.pages_var.get() or 1)),
            open_journal=self.open_journal_var.get(),
            reopen_known=self.reopen_var.get(),
        )

        def work():
            if not self.browser.connected and not self.browser.connect():
                raise RuntimeError("Não achei a aba do Total Battle no Chrome com depuração (porta 9222).")
            self.capture = capture.CdpCapture(port=self.settings.cdp_port, out_dir=None, on_record=self.collector.feed_record)
            if not self.capture.start():
                raise RuntimeError("Não consegui escutar o tráfego do jogo.")
            time.sleep(1.5)  # the listener needs a moment to attach before anything is clicked
            self.browser.focus_tab()
            self.runner = mapper.JournalMapper(
                self.browser, self.calibration, self.ocr, self.collector, options,
                known_names=set(self.known_names.keys()),
                on_log=lambda text: self.inbox.put({"log": text}),
                on_card=lambda card: self.inbox.put({"card": card}),
            )
            try:
                return self.runner.run()
            finally:
                self.capture.stop()
                self.capture = None

        def done(result, error):
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            if error:
                self._write_log("ERRO: " + error)
                self._set_status("O mapeamento parou com erro.")
                messagebox.showerror("Mapeamento", error)
            mapped = [c for c in self.cards if c.status == mapper.STATUS_MAPPED]
            for text in mapper.conflicts(self.cards):
                self._write_log("CONFLITO: " + text)
            if not error:
                self._set_status(f"Fim: {len(mapped)} torneio(s) mapeado(s), {len(self.cards)} card(s) de torneio vistos.")
            self.send_btn.configure(state="normal" if mapped else "disabled")
        self._in_thread(work, done)

    def _stop(self) -> None:
        if self.runner is not None:
            self.runner.stop()
        self._set_status("Parando depois do passo atual...")

    def _add_card(self, card: mapper.MappedCard) -> None:
        self.cards.append(card)
        ended = journal.Tournament("", ended_at=card.ended_at).ended_at_text if card.ended_at else ""
        self.tree.insert("", "end", values=(
            card.page, card.card, card.name, card.tournament_key or "", ended,
            "sim" if card.icon_png else "", card.status,
        ))
        self.tree.yview_moveto(1.0)

    # ------------------------------------------------------------------ send
    def _send(self) -> None:
        entries = mapper.catalogue_entries(self.cards)
        problems = mapper.conflicts(self.cards)
        if problems and not messagebox.askyesno(
            "Conflitos", "Há leituras em conflito:\n\n" + "\n".join(problems) + "\n\nEnviar assim mesmo?"
        ):
            return

        tournaments: List[dict] = []
        if self.send_rankings_var.get():
            names = {c.result_uid: c.name for c in self.cards if c.status == mapper.STATUS_MAPPED and c.result_uid}
            for t in self.collector.tournaments():
                if t.result_uid in names and t.has_ranking:
                    tournaments.append(journal.build_payload(t, names[t.result_uid], t.ended_at_iso, APP_VERSION))
            # Oldest first, so the newest tournament is the last to set who is active.
            tournaments.sort(key=lambda p: p.get("ended_at") or "")

        client = self._client()
        self.send_btn.configure(state="disabled")
        self._set_status("Enviando ao site...")

        def work():
            catalog = client.send_catalog(entries) if entries else []
            sent = []
            for payload in tournaments:
                try:
                    reply = client.send_tournament(payload)
                    sent.append(f"#{reply.get('event_number')} {reply.get('event_name')}: "
                                + ("cadastrado" if reply.get("event_created") else "já existia"))
                except api.ApiError as exc:
                    sent.append(f"{payload['result_uid'][:8]}: {exc.message}")
            return catalog, sent

        def done(result, error):
            self.send_btn.configure(state="normal")
            if error:
                messagebox.showerror("Envio", error)
                self._set_status("O envio falhou.")
                return
            catalog, sent = result
            lines = []
            for item in catalog:
                what = "novo" if item.get("created") else ("nome atualizado" if item.get("renamed") else "sem mudança")
                if item.get("name_kept"):
                    what += " (nome definido à mão no site foi mantido)"
                if item.get("image_saved"):
                    what += ", imagem salva"
                lines.append(f"Tipo {item.get('game_type')}: {item.get('name')} - {what}")
            lines += sent
            for line in lines:
                self._write_log(line)
            self._set_status(f"Enviado: {len(catalog)} tipo(s) de torneio" + (f", {len(sent)} ranking(s)" if sent else "") + ".")
            messagebox.showinfo("Enviado", "\n".join(lines[:40]) or "Nada a enviar.")
        self._in_thread(work, done)

    def shutdown(self) -> None:
        if self.runner is not None:
            self.runner.stop()
        if self.capture is not None and self.capture.is_running:
            self.capture.stop()
        self.browser.disconnect()
