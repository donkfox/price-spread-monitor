package com.pricemonitor;

import com.android.uiautomator.core.Configurator;
import com.android.uiautomator.core.FastHierarchyDumper;
import com.android.uiautomator.testrunner.UiAutomatorTestCase;

public final class FastDump extends UiAutomatorTestCase {
    public void testDumpWithoutIdleWait() {
        Configurator.getInstance().setWaitForIdleTimeout(0);
        FastHierarchyDumper.dump("/sdcard/price-monitor-window.xml");
    }
}
