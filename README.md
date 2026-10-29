# Painel do Polo do Baixo Amazonas — DPE-AM

Painel interno da equipe do Polo do Baixo Amazonas (Parintins, Nhamundá e UDI Barreirinha): quem responde por cada defensoria, afastamentos, grade semanal do planejamento, férias da equipe, plantão e atos do Diário Oficial.

Endereço: https://pbaixoamazonas.github.io/polobaixoamazonasdpeam/

## Onde ficam as coisas

| Arquivo | O que é |
|---|---|
| `index.html` | O site inteiro (uma página). A configuração do Firebase fica no topo do script, em `FIREBASE_CONFIG`. |
| `firestore.rules` | Regras de acesso do banco. Copiar para Firebase › Firestore › Regras sempre que mudar. |
| `diario.json` | Atos do Diário Oficial lidos pelo robô. Atualizado sozinho. |
| `robo/diario.py` | O robô do Diário. |
| `robo/defensores.json` | Nomes dos defensores que o robô procura nas portarias. Atualizar quando entrar ou sair alguém. |
| `robo/estado.json` | Última edição lida. |
| `.github/workflows/diario.yml` | Agenda do robô: todo dia às 05:00 de Manaus. |

Nenhum dado pessoal fica neste repositório, que é público: pessoas, escalas, afastamentos e planejamento ficam no Firebase, só com login.

## Rodar o robô na mão

Aba **Actions** › **Diário Oficial** › **Run workflow**. Em "Reler a partir desta edição" dá para informar um número para reler edições antigas.

O robô precisa do segredo `ANTHROPIC_API_KEY` (Settings › Secrets and variables › Actions).

## Acesso

- Quem lê: quem está em **Pessoas do polo** com o e-mail funcional.
- Quem edita: perfil **Admin** em Pessoas do polo, e a conta polobaixoamazonas@defensoria.am.def.br.
- Contas de login: cada pessoa cria a própria senha em **Primeiro acesso** e confirma pelo link enviado ao e-mail funcional. Só lê quem está em Pessoas do polo e confirmou o e-mail. A conta do polo foi criada no Firebase pelo console.
