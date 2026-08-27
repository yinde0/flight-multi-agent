# Vertical Slice 04: Weather Corroboration

This slice adds weather as independent evidence without allowing a forecast to
notify a traveler by itself.

```text
                                      +--> Flight Status MCP --> AviationStack
Travel API --> A2A Monitoring Agent --|
                                      +--> Weather MCP -------> OpenWeatherMap
                                                  |
                                      DynamoDB flight + weather state
                                                  |
                                      disruption_candidate (NATS)
                                                  |
                                             Eval Agent
                                      SUPPRESS or disruption_confirmed
```

## Component boundaries

- **Weather MCP** owns the OpenWeather request and response shape. It exposes only
  `get_airport_weather(airport, target_at, replay_key)` and has no notification,
  search, booking, or cancellation capability.
- **Airport coordinates** come from the versioned local
  `travel_eval/fixtures/airports.json` registry. Provider-side city geocoding is
  not used, so an IATA code always resolves to the same point.
- **Monitoring Agent** asks for the forecast nearest scheduled departure. It
  compares meaningful weather fields (`risk_level` and `alerts`) with the last
  weather record instead of treating every provider refresh as a new disruption.
- **DynamoDB** stores `LAST_OBSERVATION` and `LAST_WEATHER` separately for each
  trip leg. Restarting the Monitoring Agent therefore does not erase either
  baseline.
- **Eval Agent** remains the only authority that can publish
  `disruption_confirmed`. The monitor merely says whether severe weather
  corroborates an operational flight disruption.

The live provider calls OpenWeather's 5-day / 3-hour forecast endpoint by
latitude and longitude:

```text
GET https://api.openweathermap.org/data/2.5/forecast
    ?lat=...
    &lon=...
    &units=metric
    &appid=...
```

The provider selects the forecast step closest to departure, rejects a step more
than three hours away, normalizes weather condition codes, and never returns the
API key. See the official [5-day forecast documentation](https://openweathermap.org/api/forecast5)
and [condition-code table](https://openweathermap.org/api/weather-conditions).

## Versioned policy behavior

Policy `1.2.0` applies these rules:

| Evidence | Eval result | Reason |
|---|---|---|
| Light or moderate weather only | `SUPPRESS` | `MINOR_WEATHER_ONLY` |
| High or severe weather only | `SUPPRESS` | `WEATHER_UNCORROBORATED` |
| Weather clears with no new flight impact | `SUPPRESS` | `WEATHER_CLEARED` |
| Delay of 30–89 minutes | `NOTIFY` | `DELAY_NOTIFY_THRESHOLD` |
| Same delay plus high/severe weather | same verdict | adds `SEVERE_WEATHER_CORROBORATED` |
| Weather MCP unavailable | flight-only policy | no fabricated weather evidence |

Weather does not lower the 30-minute delay threshold. It explains supporting
evidence; it does not manufacture operational impact.

## Seven-poll golden replay

| Poll | Flight | Weather | Expected result |
|---:|---|---|---|
| 1 | on time | clear | store both baselines |
| 2 | unchanged | light rain | suppress minor weather-only candidate |
| 3 | unchanged | severe thunderstorm | suppress uncorroborated candidate |
| 4 | delayed 45 minutes | severe thunderstorm | notify, weather-corroborated |
| 5 | unchanged | unchanged | no candidate |
| 6 | same delay | clear | suppress weather-cleared candidate |
| 7 | unchanged | provider outage | continue flight-only, no candidate |

Run the complete container path and restart only the Monitoring Agent after its
baseline:

```powershell
docker compose -f compose.yaml -f compose.test.yaml -f compose.weather-test.yaml up --build -d --wait
.\.venv\Scripts\python.exe tools\run_vertical_weather_test.py --restart-monitor
docker compose -f compose.yaml -f compose.test.yaml -f compose.weather-test.yaml down
```

The runner passes only when every observed decision-critical field equals
`vertical_04_expected.json`. Runtime output never rewrites that golden file.

## Live provider smoke test

Put `OpenWeatherMap_API_KEY` in the ignored `.env` file, start the base stack,
then ask the Monitoring Agent container to call the Weather MCP. The key remains
inside the Weather MCP container:

```powershell
docker compose up --build -d --wait
docker compose exec -T monitor-agent python -m flight_agent.live_weather_cli LHR
```

The command defaults to six hours from now, which is safely inside the 5-day
forecast window. An explicit ISO-8601 target can be supplied as the second
argument.

## Failure rules

- Flight-status failure still fails closed with `poll_failed`.
- Weather failure is reported as `WEATHER_MCP_FAILED`, but flight diffing and Eval
  continue.
- Failed weather reads never overwrite `LAST_WEATHER`.
- Unsupported airport codes fail weather lookup explicitly; they are not guessed.
- A weather-only candidate can never create `disruption_confirmed` under policy
  `1.2.0`.
- This slice still sends no notification and starts no rebooking search.
