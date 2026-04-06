# AI Agent Copilot — instrukce pro Claude

Tento projekt poskytuje MCP server `copilot-delegate` s nástroji pro delegování úloh na GitHub Copilot CLI.
Nástroje jsou automaticky konfigurovány z `config.yaml` při startu MCP serveru.

## Automatické použití

V repu jsou projektové Claude subagenty v `.claude/agents/`. Používej je proaktivně, když task odpovídá jejich zaměření:

- `copilotsimple` pro jednoduché implementace, boilerplate a malé refaktory
- `copilotsecurity` pro security analýzu
- `copilotcodereview` pro code review

Jména jsou záměrně bez pomlček, aby šla pohodlně mentionovat přes `@` bez vkládaných uvozovek.

Tyto subagenty mají jako podkladové vykonání používat MCP nástroje z `copilot-delegate`. `run_agent_*` jsou tedy podkladové tools, ne samostatní Claude agents.

## Podkladové MCP nástroje

### `run_agent_simple`
Použij pro jednoduché vývojářské úkoly:
- Generování kódu a boilerplate
- Malé refaktory (přejmenování, extrakce funkce)
- Vysvětlení existujícího kódu
- Jednoduché shell příkazy a skripty
- Formátování a syntaktické opravy

**Nepoužívej pro:** architektonická rozhodnutí, security témata, autentizaci/autorizaci.

### `run_agent_security`
Použij pro bezpečnostní analýzu:
- Analýza kódu na bezpečnostní zranitelnosti (OWASP Top 10)
- Review závislostí z hlediska bezpečnosti
- Threat modeling dílčích komponent
- Kontrola konfiguračních souborů (CORS, CSP, TLS)

**Poznámka:** Výstup je doporučení, finální rozhodnutí patří vývojáři nebo bezpečnostnímu týmu.

### `run_agent_code_review`
Použij jako doplněk při code review:
- Kontrola kvality kódu, čitelnosti, naming conventions
- Detekce komplexity a návrhových antipatternů
- Ověření pokrytí testy
- Dodržování best practices (DRY, SOLID, apod.)

**Poznámka:** Nahrazuje mechanické kontroly, nezahrnuje business logiku ani architekturu.

## Kdy NEpoužívat tyto nástroje

- Architektonická rozhodnutí a návrh systémů
- Finální security rozhodnutí (pouze jako vstup)
- Compliance a právní otázky
- Multi-tenant design
- Ladění produkčních incidentů

## Přidání nového profilu

Upravit `~/.local/share/ai-agent/copilot/config.yaml` a spustit:
```bash
./install-copilot-agent.sh --update
```
Poté restartovat Claude Code.
