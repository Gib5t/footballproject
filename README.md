# Football Analyst: free, self-updating prediction site

Every day a free GitHub robot downloads the latest results, re-fits the model, writes new predictions (1X2, Over/Under 2.5, HT/FT, small/medium/big betslips with explanations) and updates a **public track record** that scores every past pick. Cost: 0.

## Set up (about 20 minutes, no card needed)
1. Get a free API key at https://www.football-data.org/client/register (fixtures and results for the five major European leagues).
2. For player, corner, and card projections, also get a free key at https://pitchapi.dev/get-api-key. This is optional; core predictions and the daily slip still work without it.
3. Create a free GitHub account and a **new public repository**. Upload everything in this folder (keep the `.github` folder).
4. Repo → Settings → Secrets and variables → Actions → **New repository secret**. Add `FOOTBALL_DATA_KEY` with your football-data.org key, and optionally `PITCHAPI_KEY` with your PitchAPI key. Never put either key in a public file or the webpage.
5. Repo → Settings → Pages → Source: **GitHub Actions**.
6. Repo → Actions → "update-predictions" → **Run workflow**. Wait for the green check mark.
7. Open the site URL shown by the workflow, or use Settings → Pages → **Visit site**. It appears at `https://YOURNAME.github.io/REPO/`.

It then refreshes by itself twice a day. Test offline anytime with `python scripts/update.py --demo`.

## How it predicts (no bookmaker odds, no outside tips)
Time-weighted Poisson model: home/away attack and defence strengths (shrunk toward league averages), small recent-form adjustment, a Poisson score grid for 1X2 and Over/Under, and a two-half simulation for HT/FT using the league's real first-half goal share. Each pick has a plain-English reason. The daily slip uses a maximum of three picks from different matches and publishes its result in the track record. Combined probabilities are approximate.

When `PITCHAPI_KEY` is configured, the updater adds recent player scorer/assist estimates and team corner/card totals where match statistics are available. Player estimates use recent player xG/goals, assists, and minutes; lineups and availability can change. Missing data stays blank rather than being shown as zero. Numeric corner/card forecasts are tracked with mean absolute error.

## Making money, honestly
- **The model won't print money.** Bookmaker markets are very efficient and take ~5% margin. Expect roughly market-level accuracy at best. Don't bet your own money on it, and never sell "guaranteed" tips.
- **Your edge is the business, not the bets:** a transparent, verified track record is rare. Build trust first (3 to 6 months of logged results), and promote on Instagram/TikTok/Telegram in French/Arabic.
- **Revenue options once you have traffic:** display ads (AdSense policies on gambling content are strict: check them), affiliate links to licensed operators, or a paid tier for extras (more leagues, alerts, deeper stats) once the track record justifies it.
- **Check the law.** Gambling advertising and affiliate marketing are restricted in many countries, Morocco included. Confirm what is allowed for you, and keep the 18+ and responsible-gambling notices.
- **Safer income in parallel:** your web-building skills already earn from clients; treat this as a long-term side project.

## Upgrade ideas
Injury/news feeds, confirmed-lineup refreshes near kickoff, more leagues, Telegram alerts, and a longer sample before drawing conclusions about performance.
