package runtimecontract

import "testing"

func TestSecretName(t *testing.T) {
	got := SecretName("a1b2c3")
	want := "msess-a1b2c3-env"
	if got != want {
		t.Fatalf("SecretName() = %q, want %q", got, want)
	}
}

func TestChildName(t *testing.T) {
	got := ChildName("a1b2c3")
	want := "msess-a1b2c3"
	if got != want {
		t.Fatalf("ChildName() = %q, want %q", got, want)
	}
}

func TestRuntimeIDFromSecretNameRoundTrip(t *testing.T) {
	for _, id := range []string{"a1b2c3", "11111111-1111-1111-1111-111111111111"} {
		got, ok := RuntimeIDFromSecretName(SecretName(id))
		if !ok {
			t.Fatalf("RuntimeIDFromSecretName(SecretName(%q)) ok = false, want true", id)
		}
		if got != id {
			t.Fatalf("RuntimeIDFromSecretName(SecretName(%q)) = %q, want %q", id, got, id)
		}
	}
}

func TestRuntimeIDFromSecretNameRejectsUnrelatedNames(t *testing.T) {
	for _, name := range []string{"", "env", "msess-", "-env", "msess-env", "other-secret", "msess-a1b2c3"} {
		if _, ok := RuntimeIDFromSecretName(name); ok {
			t.Errorf("RuntimeIDFromSecretName(%q) ok = true, want false", name)
		}
	}
}
