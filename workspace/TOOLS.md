<tools>
  <!-- 文件分工：补充说明工具的非显然约束、推荐用法和当前语义；不是完整 API 文档。 -->
  <description>Tool signatures are provided automatically via function calling. This file documents non-obvious constraints, current semantics, and usage patterns.</description>

  <tool name="exec">
    <safety>
      <limit>Commands have a configurable timeout (default 60s)</limit>
      <limit>Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)</limit>
      <limit>Output is truncated at 10,000 characters</limit>
      <limit>`restrictToWorkspace` config can limit file access to the workspace</limit>
    </safety>
  </tool>

  <tool name="cron">
    <description>Use the cron tool or `nano-alice cron ...` CLI to manage the agent's internal scheduled jobs. Cron is primarily an internal scheduler / self-wakeup mechanism: when a job becomes due, it should be treated as an internal reminder intent/event flow rather than an ordinary inbound user message. It can be used to implement reminders, recurring tasks, and delayed follow-ups.</description>
    <examples>
      <example command="nano-alice cron add --name 'morning' --message 'Good morning!' --cron '0 9 * * *'">Recurring: every day at 9am</example>
      <example command="nano-alice cron add --name 'standup' --message 'Standup time!' --cron '0 10 * * 1-5' --tz 'Asia/Shanghai'">With timezone</example>
      <example command="nano-alice cron add --name 'water' --message 'Drink water!' --every 7200">Recurring: every 2 hours</example>
      <example command="nano-alice cron add --name 'meeting' --message 'Meeting starts now!' --at '2030-01-01T15:00:00'">One-time: specific ISO time in the future (replace with your actual target time)</example>
      <example command="nano-alice cron list">List jobs</example>
      <example command="nano-alice cron run &lt;job_id&gt;">Run a job immediately for verification</example>
      <example command="nano-alice cron remove &lt;job_id&gt;">Remove a job</example>
    </examples>
    <notes>
      <note>For periodic background checks that the agent should re-read on each heartbeat tick, keep the standing instructions in `HEARTBEAT.md` at the workspace root.</note>
      <note>`HEARTBEAT.md` is the heartbeat workflow entrypoint: store durable polling instructions, update them when the task changes, and remove or clear them when the task is finished.</note>
    </notes>
  </tool>

  <tool name="message">
    <description>Sending a message is not the same as confirmed delivery. Prefer returning or recording message identifiers / receipts when the channel supports them.</description>
  </tool>
</tools>
