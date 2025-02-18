# EV Charging Time Optimizer Blueprint (For Zaptec and Nordpool)

This Home Assistant blueprint provides intelligent EV charging optimization based on electricity prices. It automatically determines the most cost-effective charging schedule while ensuring your EV is ready for your next departure.

## Prerequisites
- Home Assistant with Nordpool integration
- Compatible EV charger (e.g., Zaptec)
- EV with SOC reporting capability

## Operation
For every run, the blueprint will:
1. Calculates required charging time based on current SOC and target
2. Identifies the cheapest available hours before departure
3. Enables charging during optimal hours
4. Updates the scheduled start time in the helper entity (so that you can show when the charging will start)

The charger will only operate during the identified cheapest hours until the target SOC is reached.

## How It Works

The optimizer continuously monitors:
1. Current battery state of charge
2. Electricity prices (via Nordpool integration)
3. Time until departure
4. Required charging duration


### Price Selection Algorithm

The algorithm follows these precise steps:

1. **Calculate Required Charging Hours**
   ```
   Current SOC: 30% → Target SOC: 80%
   Battery Capacity: 77 kWh
   Charging Power: 11 kW
   ────────────────────────────────────
   Required Energy = (80% - 30%) × 77 kWh = 38.5 kWh
   Charging Time = 38.5 kWh ÷ 11 kW = 3.5 hours
   ```

2. **Filter Available Hours**
   ```
   Current Time ─────┐
                    ▼
   ┌────────────────────────────────────────────┐
   │ All Price Hours (24h)                      │
   ├────┬────┬────┬────┬────┬────┬────┬────┬────┤
   │ 1  │ 2  │ 3  │ 4  │ 5  │ 6  │ 7  │ 8  │... │
   └────┴────┴────┴────┴────┴────┴────┴────┴────┘
                    │
                    │ Remove past hours &
                    │ hours after departure
                    ▼
   ┌────────────────────────────┐
   │ Valid Price Hours          │
   ├────┬────┬────┬────┬────┬───┤
   │ 4  │ 5  │ 6  │ 7  │ 8  │   │
   └────┴────┴────┴────┴────┴───┘
   ```

3. **Sort by Price**
   ```
   Price (€/kWh) →  0.15  0.12  0.18  0.10  0.20
   Hour          →   4     5     6     7     8
                    │
                    │ Sort ascending
                    ▼
   ┌────────────────────────┐
   │ Price-Sorted Hours     │
   ├────┬────┬────┬────┬────┐
   │ 7  │ 5  │ 4  │ 6  │ 8  │  ← Hours
   ├────┼────┼────┼────┼────┤
   │0.10│0.12│0.15│0.18│0.20│  ← Price €/kWh
   └────┴────┴────┴────┴────┘
   ```

4. **Select Required Hours**
   ```
   Required: 3.5 hours (rounded up to 4)
                    │
                    │ Take first 4 cheapest
                    ▼
   ┌───────────────────┐
   │ Selected Hours    │
   ├────┬────┬────┬────┐
   │ 7  │ 5  │ 4  │ 6  │    ← Cheapest 4 hours
   └────┴────┴────┴────┘
   ```

5. **Sort by Time**
   ```
                    │
                    │ Sort chronologically
                    ▼
   ┌───────────────────┐
   │ Final Schedule    │
   ├────┬────┬────┬────┐
   │ 4  │ 5  │ 6  │ 7  │    ← Hours in sequence
   └────┴────┴────┴────┘
   ```

6. **Check Current Time Position**
   ```
   Current Time: 4:30
                    │
                    │ Check if within first period
                    ▼
   ┌───────────────────┐
   │ Time Check        │
   │  ▼                │
   │ 4  │ 5  │ 6  │ 7  │ │ → Currently in first period
   └────┴────┴────┴────┘ │ → Charging should be active
   ```

The algorithm ensures:
- Price selection ability to split charging into multiple periods to avoid spikes
- Continuous charging periods when possible
- Immediate charging if currently in the first optimal period
- All charging completed before departure time



## Configuration

### Required Inputs

- `battery_capacity`: Your EV's battery capacity in kWh
- `departure_time`: When you plan to use the car next
- `target_soc`: Desired battery percentage
- `car_soc_entity`: Sensor reporting current battery level
- `charger_mode_entity`: Charger status sensor
- `price_entity`: Electricity price sensor (Nordpool)
- `max_charge_power_entity`: Maximum charging current setting
- `min_charge_power_entity`: Minimum charging current setting
- `charge_switch_entity`: Switch to control charging
- `helper_charge_start_entity`: Helper entity (`input_datetime`) to store the next charging start time
- `helper_charge_departure_entity`: Optional helper entity (`input_datetime`) to dynamically set departure time
- `helper_charge_hours_entity`: Helper entity (`input_text`) to store charging schedule information and cost

### Helper Entity Setup

Before using this blueprint, you'll need to create the following helper entities:

1. Create an `input_datetime` helper for `helper_charge_start_entity`:
   - This will store when the next charging session is scheduled to start
   - The automation will update this automatically

2. Optional: Create an `input_datetime` helper for `helper_charge_departure_entity`:
   - Only needed if you want to dynamically change departure times
   - If not set, the blueprint will use the fixed departure time from the configuration

3. Create an `input_text` helper for `helper_charge_hours_entity`:
   - This will store the charging schedule details and cost information
   - Used for displaying charging information in your dashboard
   - The automation will update this with the selected charging hours and total cost


