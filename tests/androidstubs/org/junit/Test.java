package org.junit;
/** Stub — see tests/test_android_device_tests_compile.py for why these exist. */
public @interface Test {
  /** Matches JUnit 4's sentinel so device tests can compile real expected-exception assertions. */
  class None extends Throwable {
    private None() { }
  }

  Class<? extends Throwable> expected() default None.class;
  long timeout() default 0L;
}
