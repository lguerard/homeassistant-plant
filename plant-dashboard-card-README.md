Plant Dashboard Card

Installation

1. Copy `plant-dashboard-card.js` to `www/community/plant-dashboard-card/plant-dashboard-card.js`.
2. Add the resource in Lovelace:

```yaml
resources:
  - url: /local/community/plant-dashboard-card/plant-dashboard-card.js
    type: module
```

3. Add the card to a Lovelace view:

```yaml
type: custom:plant-dashboard-card
show_all: true
# options:
# sort_by: watering | name | nickname
# confirm_before_done: true  # shows a confirm dialog when pressing Done
```

Notes

- If you update the card file, clear browser cache or use 'Reload resources' in the Lovelace UI.
- The card uses the `plant/get_info` websocket command to find a plant's watering sensor. If your plants don't show hours-until-watering, ensure the integration created the watering sensor (sensor names ending with 'Watering').
- For accessibility and additional polish I can add ARIA labels and localized strings — tell me if you want that.
