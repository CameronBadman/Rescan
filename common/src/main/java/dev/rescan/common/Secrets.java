package dev.rescan.common;

public final class Secrets {
  private static final java.util.Map<String,Entry> CACHE=new java.util.concurrent.ConcurrentHashMap<>();
  private record Entry(String value,long until) {}
  public static String get(String direct,String arn) {
    String value=Settings.get(direct,""); if(!value.isEmpty()) return value;
    String key=Settings.get(arn,""); if(key.isEmpty()) return "";
    return CACHE.compute(key,(k,old)->{
      if(old!=null && old.until()>System.currentTimeMillis()) return old;
      try(var client=software.amazon.awssdk.services.secretsmanager.SecretsManagerClient.create()) {
        return new Entry(client.getSecretValue(r->r.secretId(k)).secretString(),System.currentTimeMillis()+60000);
      }
    }).value();
  }
}
