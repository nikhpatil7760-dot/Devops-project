const missionForm = document.querySelector("#mission-form");
const missionInput = document.querySelector("#mission-input");
const characterCount = document.querySelector("#character-count");
const missionResult = document.querySelector("#mission-result");
const runLog = document.querySelector("#run-log");
const runState = document.querySelector("#run-state");
const traceId = document.querySelector("#trace-id");

const stages = [
  ["Planner", "Turn the goal into a small set of reviewable steps."],
  ["Researcher", "Surface the context and dependencies this goal may need."],
  ["Verifier", "Check the plan for gaps before a person takes the next step."],
];

missionInput.addEventListener("input", () => {
  characterCount.textContent = `${missionInput.value.length} / 240`;
});

missionForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const mission = missionInput.value.trim();
  if (!mission) {
    missionInput.focus();
    missionInput.setAttribute("aria-invalid", "true");
    return;
  }

  missionInput.removeAttribute("aria-invalid");
  missionResult.hidden = true;
  runLog.hidden = false;
  runLog.replaceChildren();
  runState.className = "run-state is-active";
  runState.innerHTML = "<i></i> MAPPING MISSION";
  traceId.textContent = Math.random().toString(36).slice(2, 8).toUpperCase();

  const missionItem = document.createElement("li");
  missionItem.className = "mission-summary-row";
  missionItem.innerHTML = `<span class="log-index">GOAL</span><span class="log-copy"><strong></strong><span>Local simulation only. No external actions will run.</span></span><span class="log-status">RECEIVED</span>`;
  missionItem.querySelector(".log-copy strong").textContent = mission;
  runLog.append(missionItem);

  stages.forEach(([agent, detail], index) => {
    const row = document.createElement("li");
    row.innerHTML = `<span class="log-index">0${index + 1}</span><span class="log-copy"><strong></strong><span></span></span><span class="log-status">QUEUED</span>`;
    row.querySelector(".log-copy strong").textContent = agent;
    row.querySelector(".log-copy span").textContent = detail;
    runLog.append(row);

    window.setTimeout(() => {
      row.classList.add("is-running");
      row.querySelector(".log-status").textContent = "IN REVIEW";
    }, 420 * index + 180);
    window.setTimeout(() => {
      row.classList.remove("is-running");
      row.classList.add("is-done");
      row.querySelector(".log-status").textContent = "READY";
      if (index === stages.length - 1) {
        runState.className = "run-state is-complete";
        runState.innerHTML = "<i></i> PLAN READY";
      }
    }, 420 * index + 620);
  });
});