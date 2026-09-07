# Fire Detection and Autonomous Response
**D7065E - Embedded Intelligence at the Edge**

## Summary

| | |
|---|---|
| **Student** | Binay Mishra, Project Group 3 |
| **Use Case** | Fire Detection and Autonomous Response |
| **Pitch** | Detect fires through multi-sensor fusion, distinguish real fires from false alarms, and autonomously activate sprinklers, unlock fire doors, and compute evacuation routes. |
| **Repository** | github.com/applied-ai-ltu/d7065e |

## 1. Use Case

The goal is to detect fires and respond autonomously across a BuildSim building. This use case was chosen for its closed-loop structure: sensing, detection, prediction, response, and verification. It also exercises the widest range of BuildSim features: sensors, actuators, room highlights, doors, and coverage zones.


## 2. Scenario

An electrical fault in a lecture hall triggers smoke and heat. The sensors pick it up, the system classifies it as a real fire rather than an anomaly, and responds: sprinklers activate, fire doors unlock, neighbouring rooms are flagged as at risk, and evacuation routes are computed that avoid the affected area. The 3D viewer displays the information: red for fire, orange for risk, green for safe, with escape paths drawn on the map.
As conditions change, fire spreading and rooms clearing, the agent re-evaluates and updates its response. The same loop works regardless of which room or floor the fire starts on.


## 3. What will be built

- **Sensors**: smoke, CO, and temperature per room, each running as its own process and updating values in BuildSim every N seconds.
- **Actuators**: separate processes for sprinklers (activation/deactivation) and fire doors (lock/unlock), receiving commands from the control agent and reporting state back to BuildSim.
- **Data pipeline**: sensor readings flow to a time-series database, with rolling statistics and rate-of-change computed for the detector.
- **Anomaly detection model**: an isolation forest trained on sensor data to distinguish real fires from false alarms.
- **Fire-spread predictor**: looks at the room adjacency graph and current temperatures to estimate which neighbouring rooms are likely to be affected.
- **Evacuation route planner**: uses BuildSim's route API with dynamic re-routing around fire-affected zones and locked doors.
- **Control agent**: orchestrates the system by reading detection output, generating response plans, verifying safety constraints (all fire exits unlocked, sprinkler coverage adequate, evacuation routes clear), and executing the best plan.
- **Monitoring dashboard**: room highlights (red = active fire, orange = risk, green = safe), coverage zones for smoke spread, occupant tracking, and evacuation route display.

Each component runs as a separate process communicating through defined interfaces, and connects to BuildSim through its REST and WebSocket APIs.


## 4. The autonomous part

The agent runs a loop: detect, plan, check, act. When the detector flags a fire, the agent works out what to do (sprinklers, doors, routes), checks the plan against basic safety rules (fire exits must be unlocked, routes must avoid affected rooms), and then sends the commands. After acting, it checks whether the expected effect happened (temperature dropping in sprinkler rooms, occupants moving to safety). If verification fails, the plan is revised.

Starting approach: isolation forest for anomaly detection, rule-based scoring for plan selection, constraint checking for verification. The detection model refines itself after each event, re-evaluating its performance against accumulated outcomes to improve over time.


## 5. Unknowns & risks

- The simulated sensor noise needs to be realistic enough for the detector to learn something useful. The model won't generalize if the difference between "normal" and "fire" is too obvious.
- The fire spread prediction relies on the room adjacency graph and temperature readings. This may not produce realistic propagation without a more detailed thermal model behind it.
- The evacuation route planner needs to re-route as conditions change: doors locking and/or new rooms affected. Not clear yet how responsive this can be within the simulation loop.
- The system has many components. The plan is to get a single room working with the full loop first and then scale to the building. The risk is that scaling might introduce integration issues that are hard to debug across distributed processes.


## 6. Physical simulation approach

- **Smoke**: near zero baseline with Gaussian noise. During a fire event, ramps up gradually with some variation. False alarms modelled as brief, lower-intensity spikes from dust, equipment overheating, or sensor drift that do not correlate with CO or temperature.
- **CO**: rises alongside smoke during real fires because of incomplete combustion. Stays flat during false alarms. This correlation across sensors is how the detector distinguishes real fires from false alarms.
- **Temperature**: simple per-room model. Heat comes in from occupants and fire events, leaks out through walls proportional to the indoor-outdoor difference. During a fire, the source room heats up fast and neighbouring rooms follow through the adjacency graph, weaker with distance.
- **Occupancy**: weekday schedule (arrivals 07:00 - 09:00, lunch 11:30 - 13:00, departures 16:00 - 18:00). Varies by room type, with some randomness so no two days look identical. Drives the evacuation part of the system.
- **Actuator feedback**: sprinkler activation reduces temperature rise rate in the affected room. Door lock state constrains the walkable graph for evacuation routing. This closes the loop. The agent's decisions change what happens next in the simulation.


## 7. First step & rough plan

The first step is one room end-to-end: three sensors (smoke, CO, temperature) → anomaly detector → control agent → sprinkler actuator → BuildSim, to prove the detection-to-response loop closes and the viewer shows the result. After that:

- **W37-W38**: get the one-room loop working, set up the data pipeline, train the detector on normal data.
- **W39**: scale to a full floor, add fire-spread prediction and door actuators.
- **W40**: evacuation routing, occupancy tracking, dashboard.
- **W41**: self-refinement, constraint checking, failure handling.
- **W42**: evaluation, final report and submission.