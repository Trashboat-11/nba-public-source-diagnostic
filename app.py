import streamlit as st

from source_probe import run_probe

st.set_page_config(page_title="Public NBA source check", layout="centered")
st.title("Public NBA source check")
st.caption("Checks access from this server. Pregame eligibility and prediction generation require separate validation.")


@st.cache_data(ttl=600, max_entries=1, show_spinner=False)
def cached_probe():
    return run_probe()


if st.button("Check public sources", type="primary"):
    with st.spinner("Checking NBA, ESPN and SportsDataverse…"):
        st.session_state["source_result"] = cached_probe()

if result := st.session_state.get("source_result"):
    for label, passed in result["checks"].items():
        st.write(f"**{label}** · {'Passed' if passed else 'Unavailable or unverified'}")
    st.caption(f"Checked {result['observed_utc']} · {result['wall_s']:.1f} s wall · "
               f"{result['cpu_s']:.1f} s CPU · {result['process_peak_mib']:.1f} MiB peak process RSS")
    with st.expander("Request results"):
        st.dataframe(result["requests"], hide_index=True)
