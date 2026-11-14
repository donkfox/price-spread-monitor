package com.android.uiautomator.core;

import android.view.accessibility.AccessibilityNodeInfo;

import java.io.File;

public final class FastHierarchyDumper {
    private FastHierarchyDumper() {
    }

    public static void dump(String path) {
        UiDevice device = UiDevice.getInstance();
        UiAutomatorBridge bridge = device.getAutomatorBridge();
        AccessibilityNodeInfo root = bridge.getQueryController().getAccessibilityRootNode();
        AccessibilityNodeInfoDumper.dumpWindowToFile(
                root,
                new File(path),
                device.getDisplayRotation(),
                device.getDisplayWidth(),
                device.getDisplayHeight()
        );
    }
}
