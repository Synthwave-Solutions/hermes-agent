# Hermes WebUI Governance Build

Doel van deze werkfolder: een volledig implementatieplan voor multi-user governance in het bestaande Hermes frontend WebUI dashboard.

Scope:
- Google SSO / OIDC login.
- RBAC gekoppeld aan SSO-claims en SSO-groepen.
- Whitelist-first toegang: standaard niets toegestaan, alles expliciet toestaan.
- Per gebruiker/groep controle over profiles, routes/endpoints, skills, MCP servers/tools, built-in tools/toolsets, folders/workspaces, terminal/CLI commands, modellen/providers, settings, config/env, cron, plugins, gateway/system operations en usage caps.
- Admin-scherm om governance te beheren en effectieve toegang te previewen.

Bestanden:

| Bestand | Inhoud |
|---|---|
| `01-research-findings.md` | Onderzoek naar huidige Hermes surfaces en OpenWebUI RBAC-patronen. |
| `02-governance-architecture.md` | Doelarchitectuur, enforcement layers, datamodel, principal resolution. |
| `03-policy-schema.yaml` | Voorstel voor whitelist-first policy-config. |
| `04-admin-ui-spec.md` | Specificatie voor admin UI/schermen. |
| `05-implementation-plan.md` | Concrete implementatietaken, per fase en met files/tests. |
| `06-test-rollout-plan.md` | Teststrategie, security checks, migratie en rollout. |
| `07-route-enforcement-map.md` | Endpoint/resource mapping voor backend enforcement. |

Belangrijkste ontwerpkeuzes:
1. Google SSO wordt aangesloten op de bestaande Hermes dashboard-auth providerlaag, niet als los nieuw authsysteem.
2. SSO-groepen worden gesynchroniseerd naar lokale governance groups bij login.
3. Permissions zijn whitelist-first: geen impliciete toegang op basis van “ingelogd zijn”.
4. Backend enforcement is leidend. Frontend filtering is alleen UX.
5. Tool-governance wordt centraal afgedwongen in de tool-dispatchlaag, plus vroeg gefilterd in tool-schema assembly.
6. Model-governance en usage caps worden per principal en per profile gecontroleerd vóór modelkeuze/run-start.
7. Admin krijgt een Preview Access scherm, vergelijkbaar met OpenWebUI, voor effectieve rechten per user/group/profile.
