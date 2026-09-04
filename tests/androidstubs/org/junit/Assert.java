package org.junit;

/**
 * Stub. Only the overloads the device tests call — a stub that grows past that is a second copy of
 * JUnit, which is exactly the shape this repo has been bitten by.
 */
public class Assert {
  public static void assertTrue(String m, boolean c) { }
  public static void assertTrue(boolean c) { }
  public static void assertFalse(String m, boolean c) { }
  public static void assertFalse(boolean c) { }
  public static void assertNotNull(String m, Object o) { }
  public static void assertNotNull(Object o) { }
  public static void assertArrayEquals(byte[] expected, byte[] actual) { }
  public static void assertEquals(String m, Object a, Object b) { }
  public static void assertEquals(Object a, Object b) { }
  public static void assertEquals(String m, long a, long b) { }
  public static void assertEquals(long a, long b) { }
  public static void assertNotEquals(Object unexpected, Object actual) { }
  public static void fail(String m) { }
}
