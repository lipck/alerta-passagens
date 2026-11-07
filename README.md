# Alerta de Passagens ✈️

Este sistema monitora sozinho, a cada 15 minutos e 24 horas por dia, as passagens de ida e volta **saindo do Rio de Janeiro (Galeão e Santos Dumont) para qualquer lugar do mundo**. Quando um preço fica muito abaixo do normal, ele avisa no **Telegram**.

## Como funciona
1. A cada 15 minutos, o GitHub Actions roda o `monitor.py`.
2. O script busca na API grátis da Travelpayouts/Aviasales as passagens mais baratas dos próximos 8 meses.
3. Para cada destino, ele compara o preço com o "normal" daquele destino:
   - quando já tem pelo menos 3 dias de histórico, usa a mediana do histórico;
   - antes disso, compara com as outras datas disponíveis para o mesmo destino.
4. Se o preço estiver **40% ou mais abaixo do normal**, chega um alerta no Telegram com o destino, o preço, as datas de ida e volta, a companhia e os links para ver quem vende (Aviasales) e conferir no Google Flights.
5. Todo dia às 9h chega também um resumo com os 10 destinos mais baratos.

O mesmo alerta não se repete a cada 15 minutos. Ele só volta se o preço cair mais 5% ou se passarem 3 dias.

## Segredos necessários (Settings → Secrets and variables → Actions)
| Nome | O que é |
|---|---|
| `TRAVELPAYOUTS_TOKEN` | Token da API da Travelpayouts |
| `TELEGRAM_BOT_TOKEN` | Token do bot, gerado pelo @BotFather |
| `TELEGRAM_CHAT_ID` | *(opcional)* O sistema descobre sozinho depois que você manda um "oi" para o bot |

## Ajustes
Edite o bloco `env` em `.github/workflows/monitor.yml`:
- `DISCOUNT`: percentual mínimo de desconto (0.40 = 40%)
- `MONTHS_AHEAD`: quantos meses à frente buscar
- `MIN_TRIP_DAYS` / `MAX_TRIP_DAYS`: duração da viagem
- `SUMMARY_HOUR`: horário do resumo diário

## Testar
Vá em **Actions → Alerta de Passagens → Run workflow**. Com a opção de teste marcada, chega uma mensagem "Teste OK" no Telegram.

## Observações
- Os preços vêm de buscas recentes feitas no Aviasales e podem ter mudado. Sempre confirme no site antes de comprar.
- O repositório pode ser público: nenhum token fica no código, só nos Secrets do GitHub. Em repositório público, os minutos do GitHub Actions são ilimitados.
