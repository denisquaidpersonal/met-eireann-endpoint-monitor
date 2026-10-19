# Met Éireann weather app endpoint monitor

[View the live status dashboard](https://denisquaidpersonal.github.io/met-eireann-endpoint-monitor/)

This public, free GitHub Actions monitor checks the HTTP request families used by the private MetEireannDarkSky iOS app at 08:00 and 20:00 `Europe/Dublin` time. Its GitHub Pages dashboard shows the latest result and 20 recent checks for each endpoint. "Live" means the most recent scheduled check; the page marks results overdue after 14 hours.

## Coverage

- Met Éireann national and outlook JSON, 3-day Tonight XML, current observations XML, and a representative Dublin point forecast XML.
- All five regional JSON feeds and the four regional pollen/UV XML feeds requested by the Forecast screen. Dublin's supplement uses Leinster's feed.
- Inland warnings plus every county warning code used by the app.
- Active rainfall frame metadata, a tile from its latest frame, the 24-hour radar archive page, and an image listed by that archive. Dormant radar modes are excluded.

The monitor excludes feeds that are implemented in the networking layer but not requested by app screens: all-warnings, coastal, sea-area, inland-lake, and Open-Meteo UV. It checks HTTP status, basic response structure, and image format. Met Éireann JSON feeds get one fresh request after an initial 404 to account for possible CDN cache differences; the dashboard labels a recovery. **A final HTTP 404 is DOWN**, including a warning file that the app itself treats as empty. It tolerates raw control characters inside warning JSON strings in the same way as the app. It does **not** collect requests made by users' phones or publish user locations.

## GitHub setup

1. Create a **public** repository containing these files. The app repository can stay private.
2. In **Settings → Pages**, set **Build and deployment → Source** to **GitHub Actions**.
3. In your GitHub notification settings, set **Actions** to **Email** and select **Only notify for failed workflows**. Ensure the account that adds or updates the schedule is yours, as GitHub sends scheduled-workflow notifications to that account.
4. Run **Weather endpoint monitor** manually once from the Actions tab. Check the dashboard URL in its deployment and the `monitor-status` branch created for history.
5. To verify email alerting, run it manually with **test_failure** selected, confirm the failed run and red dashboard result, then run it again without that option to restore a green result.

No API keys or paid services are required. The workflow commits only `history.json` to the `monitor-status` branch. GitHub's schedule is best effort; the dashboard exposes an overdue run but cannot email if GitHub never starts the workflow.

## Local checks

```sh
python3 -m unittest discover -s monitor -p 'test_*.py'
python3 monitor/monitor.py --history /tmp/weather-monitor-history.json --site /tmp/weather-monitor-site
```

The second command sends the real public requests and exits nonzero if any check fails. Generated history and dashboard output stay outside the source tree.
