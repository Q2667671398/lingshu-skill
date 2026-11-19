
// 顶部进度条已移除（来回扫的加载条太碍眼）。保留空壳接口，免得调用点报错。
function topProgress(){
  return { indet(){}, set(){}, fake(){}, done(){} };
}
const progress = topProgress();
