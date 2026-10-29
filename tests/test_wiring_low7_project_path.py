# tests/test_wiring_low7_project_path.py — DELETED (SPEC-58 §2i).
#
# The setter/clearer it covered (ui.wiring.set_active_project_path /
# clear_active_project_path) SURVIVE — window.py still calls them on project
# open/close — but the reason this file existed was the event_cards viewer
# wiring gap (event_cards.py read CRABCAKES_ACTIVE_PROJECT_PATH and nothing
# set it). The viewer is gone; utils/image_paths.py is the surviving reader
# and its env handling is pinned by tests/test_env_divergence.py
# (TestSites::test_active_project_env_constant_and_reader).
