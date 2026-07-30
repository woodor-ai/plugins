"""Internal ``am-ctld`` service entrypoint."""

from agent_meeting.lifecycle_control.controller_process import main


if __name__ == "__main__":
    raise SystemExit(main())
