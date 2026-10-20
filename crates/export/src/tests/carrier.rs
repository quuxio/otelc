use super::*;
use opentelemetry::trace::TraceState;

#[test]
fn shared_language_vectors_validate_and_round_trip() {
    let fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../../../tests/fixtures/trace-context.json"
    ))
    .unwrap();
    assert_eq!(fixture["schema_version"], 1);
    let mut names = HashSet::new();
    for case in fixture["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        assert!(names.insert(name));
        let parents: Vec<_> = case["traceparent"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        let states: Vec<_> = case["tracestate"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        let context = extract(&parents, &states);
        let expected = &case["expected"];
        if expected.is_null() {
            assert!(context.is_none(), "{name}");
            continue;
        }
        let context = context.unwrap_or_else(|| panic!("missing context for {name}"));
        assert_eq!(
            context.trace_id().to_string(),
            expected["trace_id"].as_str().unwrap(),
            "{name}"
        );
        assert_eq!(
            context.span_id().to_string(),
            expected["parent_id"].as_str().unwrap(),
            "{name}"
        );
        assert_eq!(
            context.trace_flags().to_u8(),
            expected["flags"].as_u64().unwrap() as u8,
            "{name}"
        );
        let outgoing = inject(&context);
        assert_eq!(
            outgoing.traceparent,
            format!(
                "00-{}-{}-{:02x}",
                context.trace_id(),
                context.span_id(),
                expected["flags"].as_u64().unwrap()
            ),
            "{name}"
        );
        assert_eq!(
            outgoing.tracestate.as_deref(),
            expected["tracestate"].as_str(),
            "{name}"
        );
        let again = extract(
            &[&outgoing.traceparent],
            &[outgoing.tracestate.as_deref().unwrap_or("")],
        )
        .unwrap();
        assert_eq!(inject(&again), outgoing, "{name}");
    }
}

#[test]
fn invalid_sdk_contexts_are_not_emitted_and_sdk_state_is_bounded() {
    assert!(Context::from_sdk(&SpanContext::empty_context()).is_none());
    let pairs: Vec<_> = (0..32)
        .map(|i| (format!("key{i:02}"), "v".repeat(16)))
        .collect();
    let context = SpanContext::new(
        TraceId::from(1u128),
        SpanId::from(2u64),
        TraceFlags::new(255),
        false,
        TraceState::from_key_value(pairs.clone()).unwrap(),
    );
    let headers = inject(&Context::from_sdk(&context).unwrap());
    assert!(headers.traceparent.ends_with("-03"));
    let expected = pairs[..22]
        .iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join(",");
    assert_eq!(headers.tracestate, Some(expected));
}
