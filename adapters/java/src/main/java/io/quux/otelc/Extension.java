package io.quux.otelc;

import io.opentelemetry.javaagent.extension.AgentListener;
import io.opentelemetry.sdk.autoconfigure.AutoConfiguredOpenTelemetrySdk;
import io.opentelemetry.sdk.autoconfigure.spi.AutoConfigurationCustomizer;
import io.opentelemetry.sdk.autoconfigure.spi.AutoConfigurationCustomizerProvider;
import io.opentelemetry.sdk.metrics.Aggregation;
import io.opentelemetry.sdk.metrics.InstrumentSelector;
import io.opentelemetry.sdk.metrics.View;
import io.opentelemetry.sdk.metrics.internal.MeterConfig;
import io.opentelemetry.sdk.metrics.internal.SdkMeterProviderUtil;
import java.io.IOException;
import java.nio.file.Path;
import java.util.Arrays;

/** Official Java agent SPI integration; no second SDK or standalone Java agent. */
public final class Extension implements AutoConfigurationCustomizerProvider, AgentListener {
  @Override public void customize(AutoConfigurationCustomizer customizer) {
    LiveState state = LiveState.instance();
    customizer.addMetricExporterCustomizer((exporter, config) -> new EmissionGate(exporter, state.emission));
    customizer.addMeterProviderCustomizer((builder, config) -> {
      SdkMeterProviderUtil.setMeterConfigurator(builder, scope -> state.collecting() ? MeterConfig.enabled() : MeterConfig.disabled());
      String buckets = state.property("metrics.buckets");
      if (buckets != null) {
        builder.registerView(InstrumentSelector.builder().setName("otelc.*.duration").build(),
            View.builder().setAggregation(Aggregation.explicitBucketHistogram(
                Arrays.stream(buckets.split(",")).map(Double::valueOf).toList())).build());
      }
      return builder;
    });
  }

  @Override public void afterAgent(AutoConfiguredOpenTelemetrySdk configured) {
    LiveState state = LiveState.instance();
    state.attach(configured.getOpenTelemetrySdk().getSdkMeterProvider());
    try {
      ControlServer.start(Path.of(state.property("control.socket")), state);
    } catch (IOException error) {
      throw new IllegalStateException("Cannot start private otelc metrics control", error);
    }
  }
}
