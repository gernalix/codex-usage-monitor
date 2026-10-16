# Registro leggibile delle chat Codex

Il servizio legge incrementalmente i JSONL nativi delle conversazioni Codex e crea spiegazioni in italiano. Non gestisce task, non modifica Pn, non scrive nel DB MegaVault e non segna chat come analizzate.

- File da leggere: `~/.local/state/codex-human-log/live.md`.
- Eventi strutturati: `~/.local/state/codex-human-log/live.jsonl`.
- Stato e cursori, privati: `~/.local/state/codex-human-log/state.json`.
- Ogni voce mostra progetto, Pn verificati quando presenti, link chat, novità, prossimo passo, azione richiesta e incertezze. Dove manca l'associazione, la dichiara sconosciuta.
- Nessun dato grezzo di tool o reasoning finisce nei prompt del traduttore. Le stringhe pubbliche vengono sanificate per rimuovere credenziali riconoscibili.
- Il traduttore usa un thread Codex CLI read-only identificabile e recuperabile nell'interfaccia. Gli eventi del traduttore sono esclusi dal monitor. Il thread si rinnova periodicamente. L'AI consuma quota Codex, e il timer può essere disabilitato in qualsiasi momento.
- Gli eventi vengono letti ogni 30 secondi; le traduzioni di normale avanzamento sono raggruppate e limitate a una ogni 120 secondi per chat. Un evento urgente può essere elaborato prima. Le notifiche Fedora/Telegram riguardano solo problemi importanti o richieste di intervento, con pausa di 10 minuti per chat.
- Se la traduzione AI fallisce il sistema pubblica un estratto chiaramente marcato come non verificato, non inventa un riassunto.

## Comandi

```bash
python3 codex_human_log.py once --bootstrap-chat 01a141a3-0879-7411-9245-6b144ba5ec41
python3 codex_human_log.py once --translator plain
python3 codex_human_log.py status
python3 codex_human_log.py tail
tail -f ~/.local/state/codex-human-log/live.md
systemctl --user status codex-human-log.timer
systemctl --user disable --now codex-human-log.timer
```

L'unità systemd avvia una traduzione alla volta e non modifica il processo Codex originale. Il runtime immutabile deve essere distribuito da un commit integrato e sincronizzato di `main`, non da una branch di lavoro. La sorgente resta in `gernalix/codex-usage-monitor` (progetto MegaVault ID 8).
