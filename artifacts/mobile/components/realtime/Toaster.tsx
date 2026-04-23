import { useEffect, useState } from "react";
import {
  Animated,
  Pressable,
  StyleSheet,
  Text,
  View,
  type ViewStyle,
} from "react-native";
import { useColors } from "@/hooks/useColors";

export type ToastVariant = "default" | "success" | "warning" | "destructive";

export interface ToastInput {
  title: string;
  description?: string;
  variant?: ToastVariant;
  /** Auto-dismiss in ms. Defaults to 4500. Pass 0 to disable. */
  durationMs?: number;
}

interface ToastItem extends Required<Omit<ToastInput, "description">> {
  id: number;
  description?: string;
}

type Listener = (toasts: ToastItem[]) => void;

let _id = 0;
let _state: ToastItem[] = [];
const _listeners = new Set<Listener>();
const MAX_VISIBLE = 4;

function emit() {
  for (const l of _listeners) l(_state);
}

/**
 * Module-level imperative API so callers anywhere in the tree (queries,
 * subscribers, network layers) can fire a toast without grabbing a hook.
 */
export function toast(input: ToastInput): number {
  _id += 1;
  const id = _id;
  const item: ToastItem = {
    id,
    title: input.title,
    description: input.description,
    variant: input.variant ?? "default",
    durationMs: input.durationMs ?? 4500,
  };
  _state = [..._state.slice(-(MAX_VISIBLE - 1)), item];
  emit();
  if (item.durationMs > 0) {
    setTimeout(() => dismiss(id), item.durationMs);
  }
  return id;
}

export function dismiss(id: number): void {
  const next = _state.filter((t) => t.id !== id);
  if (next.length === _state.length) return;
  _state = next;
  emit();
}

function useToasts(): ToastItem[] {
  const [items, setItems] = useState<ToastItem[]>(_state);
  useEffect(() => {
    _listeners.add(setItems);
    return () => {
      _listeners.delete(setItems);
    };
  }, []);
  return items;
}

function ToastRow({ item }: { item: ToastItem }) {
  const c = useColors();
  const opacity = useState(() => new Animated.Value(0))[0];

  useEffect(() => {
    Animated.timing(opacity, {
      toValue: 1,
      duration: 180,
      useNativeDriver: true,
    }).start();
  }, [opacity]);

  const palette = (() => {
    switch (item.variant) {
      case "destructive":
        return { border: c.destructive, text: c.destructive };
      case "warning":
        return { border: c.warning, text: c.warning };
      case "success":
        return { border: c.success, text: c.success };
      default:
        return { border: c.border, text: c.foreground };
    }
  })();

  const containerStyle: ViewStyle = {
    backgroundColor: c.card,
    borderColor: palette.border,
  };

  return (
    <Animated.View
      style={[styles.toast, containerStyle, { opacity }]}
      testID={`mobile-toast-${item.id}`}
    >
      <Pressable
        onPress={() => dismiss(item.id)}
        style={styles.toastInner}
        accessibilityRole="button"
        accessibilityLabel={`Dismiss ${item.title}`}
      >
        <Text
          style={[styles.toastTitle, { color: palette.text }]}
          numberOfLines={1}
        >
          {item.title}
        </Text>
        {item.description ? (
          <Text
            style={[styles.toastDesc, { color: c.mutedForeground }]}
            numberOfLines={3}
          >
            {item.description}
          </Text>
        ) : null}
      </Pressable>
    </Animated.View>
  );
}

/**
 * Mount once at the root. Renders a stack of toasts pinned to the top
 * of the screen (above the system status bar inset is handled by the
 * parent SafeArea — keep this lightweight).
 */
export function ToastHost() {
  const items = useToasts();
  if (items.length === 0) return null;
  return (
    <View pointerEvents="box-none" style={styles.host} testID="mobile-toast-host">
      {items.map((item) => (
        <ToastRow key={item.id} item={item} />
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  host: {
    position: "absolute",
    top: 60,
    left: 12,
    right: 12,
    gap: 8,
    zIndex: 9999,
  },
  toast: {
    borderWidth: 1,
    borderRadius: 6,
    paddingVertical: 8,
    paddingHorizontal: 10,
  },
  toastInner: { gap: 2 },
  toastTitle: {
    fontFamily: "Inter_700Bold",
    fontSize: 11,
    letterSpacing: 0.6,
    textTransform: "uppercase",
  },
  toastDesc: {
    fontFamily: "Inter_400Regular",
    fontSize: 12,
  },
});
