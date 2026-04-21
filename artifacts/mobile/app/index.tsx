import { useAuth } from "@clerk/expo";
import { Redirect } from "expo-router";

export default function Index() {
  const { isLoaded, isSignedIn } = useAuth();
  if (!isLoaded) return null;
  return isSignedIn ? <Redirect href="/(tabs)" /> : <Redirect href="/(auth)/sign-in" />;
}
